"""The server-rendered UI, v1 (spec.md A-135, report 6.10), held to these screens:

    /ui/login, /ui/callback, /ui/logout   sign-in: the dev issuer offline; OIDC with PKCE
    /ui/                                   your tenants
    /ui/t/{t}/inbox                        open approval items: the code-written payload, its
                                           hash, its expiry and how it is undone; approve,
                                           reject, or ask for changes
    /ui/t/{t}/findings                     each subject's status, and for anything but pass or
                                           fail a reason code with the one action that clears it
    /ui/t/{t}/report/{framework}           the rollup, with pending reasons and their actions
    /ui/t/{t}/verdicts/{id}                why: the rule's own reasoning and the citations code
                                           checked; model prose only under "summary"
    /ui/t/{t}/runs, /ui/t/{t}/runs/{id}    agent runs from the run log, live over server-sent
                                           events, with a Stop button

The rules of the inbox: a missing or expired decision never acts (an expired item is not
offered); a decision names the payload hash the page showed, so a payload that changed under
the page is refused; changing a proposal means asking for changes, never editing it. The
session is the sign-in token in an HttpOnly, SameSite=Strict cookie, and every form carries a
token derived from it, so a form posted from elsewhere is refused.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from ccp import auth, credentials, tenancy
from ccp.agent import runlog, stops, threads
from ccp.api import reasons
from ccp.approvals import actions, store
from ccp.auth import dev_issuer, members
from ccp.compliance_state import reader
from ccp.config import Settings
from ccp.evaluators import verdict as verdicts
from ccp.knowledge import graph as knowledge
from ccp.storage import exceptions as stored_exceptions
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from ccp.types import VerdictStatus

COOKIE = "ccp_session"
LOGIN_COOKIE = "ccp_login"  # the OIDC state and PKCE verifier, for the callback alone
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
ui = APIRouter(prefix="/ui", include_in_schema=False)


class SignIn(Exception):
    """No valid session: the page sends the browser to sign in."""


def _settings(request: Request) -> Settings:
    found: Settings = request.app.state.settings
    return found


def csrf_for(token: str) -> str:
    return hashlib.sha256(f"csrf:{token}".encode()).hexdigest()[:32]


def _who(request: Request, token: str | None) -> auth.Principal:
    if not token:
        raise SignIn
    try:
        return auth.authenticate(_settings(request), token)
    except auth.InvalidToken as exc:
        raise SignIn from exc


def viewer(
    request: Request, ccp_session: Annotated[str | None, Cookie()] = None
) -> tuple[auth.Principal, str]:
    return _who(request, ccp_session), ccp_session or ""


Signed = Annotated[tuple[auth.Principal, str], Depends(viewer)]


class Page:
    """A signed-in member's view of one tenant."""

    def __init__(self, request: Request, tenant_id: str, signed: Signed) -> None:
        who, token = signed
        try:
            ctx = TenantContext.for_id(_settings(request), tenant_id)
        except (tenancy.UnknownTenant, tenancy.TenantUnavailable) as exc:
            raise HTTPException(404, "no such tenant") from exc
        self.request = request
        self.db = ScopedDB(request.app.state.pools.app, ctx)
        self.who = who
        self.csrf = csrf_for(token)
        role = members.role_of(self.db, who)
        if role is None:
            raise HTTPException(404, "no such tenant")
        self.role: members.Role = role

    def render(self, template: str, **values: Any) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            self.request,
            template,
            {
                "tenant": self.db.tenant,
                "who": self.who,
                "role": self.role,
                "csrf": self.csrf,
                "flash": self.request.query_params.get("flash"),
                **values,
            },
        )

    def check(self, csrf: str) -> None:
        if not secrets.compare_digest(csrf, self.csrf):
            raise HTTPException(403, "this form did not come from this session")


Tenant = Annotated[Page, Depends(Page)]


def _back(url: str, flash: str) -> RedirectResponse:
    return RedirectResponse(f"{url}?{urlencode({'flash': flash})}", status_code=303)


# -- sign-in -------------------------------------------------------------------------------


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


@ui.get("/login", response_class=HTMLResponse)
def login(request: Request) -> Response:
    settings = _settings(request)
    if settings.ccp_auth_mode == "dev":
        return TEMPLATES.TemplateResponse(request, "login.html", {"issuer": dev_issuer.DEV_ISSUER})
    if not settings.ccp_oidc_authorize_url:
        raise HTTPException(503, "CCP_OIDC_AUTHORIZE_URL is not set")
    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce()
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.ccp_oidc_audience,
            "redirect_uri": f"{settings.ccp_ui_base_url.rstrip('/')}/ui/callback",
            "scope": "openid email profile",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    response = RedirectResponse(f"{settings.ccp_oidc_authorize_url}?{query}", status_code=303)
    response.set_cookie(
        LOGIN_COOKIE, f"{state}.{verifier}", max_age=600, httponly=True, samesite="lax"
    )
    return response


@ui.post("/login")
def login_dev(request: Request, subject: Annotated[str, Form()]) -> Response:
    """The dev issuer signs anyone in, offline only (CCP_AUTH_MODE=dev)."""
    settings = _settings(request)
    if settings.ccp_auth_mode != "dev":
        raise HTTPException(404)
    return _signed_in(settings, dev_issuer.mint(settings, subject.strip()))


@ui.get("/callback")
def callback(
    request: Request,
    code: str,
    state: str,
    ccp_login: Annotated[str | None, Cookie()] = None,
) -> Response:
    settings = _settings(request)
    expected, _, verifier = (ccp_login or "").partition(".")
    if not expected or not secrets.compare_digest(expected, state):
        raise HTTPException(400, "the sign-in did not start here")
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": f"{settings.ccp_ui_base_url.rstrip('/')}/ui/callback",
        "client_id": settings.ccp_oidc_audience,
        "code_verifier": verifier,
    }
    secret = credentials.secret(settings, "oidc:client")
    http: httpx.Client = request.app.state.http
    url = str(settings.ccp_oidc_token_url)
    if secret is not None:  # a confidential client also proves itself; PKCE either way
        basic = httpx.BasicAuth(settings.ccp_oidc_audience, secret)
        answer = http.post(url, data=form, auth=basic, timeout=10.0)
    else:
        answer = http.post(url, data=form, timeout=10.0)
    if answer.status_code != 200 or "id_token" not in answer.json():
        raise HTTPException(401, "the identity provider did not sign you in")
    token = str(answer.json()["id_token"])
    try:
        auth.authenticate(settings, token)
    except auth.InvalidToken as exc:
        raise HTTPException(401, f"sign-in token refused: {exc}") from exc
    response = _signed_in(settings, token)
    response.delete_cookie(LOGIN_COOKIE)
    return response


def _signed_in(settings: Settings, token: str) -> RedirectResponse:
    response = RedirectResponse("/ui/", status_code=303)
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=settings.ccp_ui_base_url.startswith("https://"),
        max_age=8 * 3600,
    )
    return response


@ui.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/ui/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@ui.get("/", response_class=HTMLResponse)
def home(request: Request, signed: Signed) -> HTMLResponse:
    who, _ = signed
    pools = request.app.state.pools
    mine = []
    for ctx in TenantContext.all(_settings(request)):
        role = members.role_of(ScopedDB(pools.app, ctx), who)
        if role is not None:
            mine.append(
                {"tenant_id": ctx.tenant_id, "name": ctx.settings.display_name, "role": role}
            )
    return TEMPLATES.TemplateResponse(request, "home.html", {"who": who, "tenants": mine})


# -- the inbox -----------------------------------------------------------------------------


def _undo(item: store.Approval) -> str:
    risk = item.payload.get("risk")
    if isinstance(risk, dict) and risk.get("undo"):
        return str(risk["undo"])
    return {
        "tier3_interrupt": "a later evaluation replaces the verdict",
        "policy_draft": "a later draft replaces it",
        "exception": "an approver may end it early",
        "identity_merge": "a correction is a new identity review",
        "scope_change": "another change of scope",
        "break_glass_review": "nothing to undo: it records a judgement",
    }.get(item.kind, "see the payload")


@ui.get("/t/{tenant_id}/inbox", response_class=HTMLResponse)
def inbox(page: Tenant) -> HTMLResponse:
    now = datetime.now(UTC)
    items = [
        {
            "item": i,
            "payload": json.dumps(i.payload, indent=2, sort_keys=True, default=str),
            "undo": _undo(i),
            "mine": i.requested_by == page.who.principal_id,
        }
        for i in store.list_items(page.db, open_only=True)
        if not i.expired(now)  # an expired item acts on nothing, so it is not offered
    ]
    return page.render("inbox.html", items=items)


@ui.post("/t/{tenant_id}/approvals/{approval_id}/{action}")
def decide(
    approval_id: str,
    action: str,
    page: Tenant,
    csrf: Annotated[str, Form()],
    payload_sha256: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
) -> RedirectResponse:
    page.check(csrf)
    back = f"/ui/t/{page.db.tenant.tenant_id}/inbox"
    settings = _settings(page.request)
    try:
        if action == "approve":
            done = actions.approve(
                page.db, settings, approval_id, page.who, note or None, seen_sha256=payload_sha256
            )
        elif action == "reject":
            done = actions.reject(
                page.db,
                settings,
                approval_id,
                page.who,
                note or "rejected",
                seen_sha256=payload_sha256,
            )
        elif action == "request-changes":
            current = store.get(page.db, approval_id)
            if current is None or current.payload_sha256 != payload_sha256:
                return _back(
                    back, "the item changed since the page showed it; nothing was recorded"
                )
            done = actions.request_changes(page.db, approval_id, page.who, note or "change it")
        else:
            raise HTTPException(404)
    except (LookupError, ValueError, PermissionError, actions.PayloadChanged) as exc:
        return _back(back, f"refused: {exc}")
    return _back(back, f"{done.approval.state}: {done.detail}")


# -- findings, report, verdicts ------------------------------------------------------------


@ui.get("/t/{tenant_id}/findings", response_class=HTMLResponse)
def findings(page: Tenant) -> HTMLResponse:
    held = stored_exceptions.excepted_until(page.db)
    rows = []
    for head in reader.heads(page.db):
        why: list[reasons.Reason] = []
        if head.status not in (VerdictStatus.passed, VerdictStatus.failed):
            found = verdicts.load(page.db, head.verdict_id)
            why = reasons.for_events(list(found.guardrail_events) if found else [])
            why = why or [reasons.Reason(head.status.value, *reasons.GENERIC)]
        rows.append(
            {
                "head": head,
                "shown": stored_exceptions.label(head.status.value, held.get(head.subject_key)),
                "reasons": why,
            }
        )
    return page.render("findings.html", rows=rows)


@ui.get("/t/{tenant_id}/report/{framework_id}", response_class=HTMLResponse)
def report(framework_id: str, page: Tenant) -> HTMLResponse:
    if framework_id not in knowledge.load().frameworks:
        raise HTTPException(404, "no such framework")
    result = reader.framework_rollup(page.db, framework_id)
    rows = [
        {"r": r, "reasons": [reasons.for_pending(code) for code in sorted(r.pending_reasons)]}
        for r in result.requirements
    ]
    return page.render("report.html", result=result, rows=rows)


@ui.get("/t/{tenant_id}/verdicts/{verdict_id}", response_class=HTMLResponse)
def verdict(verdict_id: str, page: Tenant) -> HTMLResponse:
    found = verdicts.load(page.db, verdict_id)
    if found is None:
        raise HTTPException(404, "no such verdict")
    return page.render(
        "verdict.html",
        v=found,
        checked=[c for c in found.citations if c.verified],
        unchecked=[c for c in found.citations if not c.verified],
        reasons=reasons.for_events(list(found.guardrail_events)),
        model_written=found.tier != 1,
    )


# -- runs ----------------------------------------------------------------------------------


@ui.get("/t/{tenant_id}/runs", response_class=HTMLResponse)
def runs(page: Tenant) -> HTMLResponse:
    return page.render("runs.html", runs=threads.all_threads(page.db, limit=50))


@ui.get("/t/{tenant_id}/runs/{thread_id}", response_class=HTMLResponse)
def run(thread_id: str, page: Tenant) -> HTMLResponse:
    row = threads.get(page.db, thread_id)
    if row is None:
        raise HTTPException(404, "no such run")
    return page.render(
        "run.html",
        run=row,
        steps=runlog.steps_of(page.db, thread_id),
        stopped_by=stops.requested(page.db, thread_id),
    )


LIVE = ("running", "interrupted")


@ui.get("/t/{tenant_id}/runs/{thread_id}/events")
async def run_events(thread_id: str, page: Tenant) -> StreamingResponse:
    """Each recorded step as it lands, read from the run log, never the model's narration;
    the stream ends when the run does."""

    async def stream() -> AsyncIterator[str]:
        seen = -1
        while True:
            for step in runlog.steps_of(page.db, thread_id, after=seen):
                seen = int(step["seq"])
                yield f"data: {json.dumps(step, default=str)}\n\n"
            row = threads.get(page.db, thread_id)
            if row is None or row.status not in LIVE:
                status = row.status if row is not None else "unknown"
                yield f"event: end\ndata: {json.dumps({'status': status})}\n\n"
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(stream(), media_type="text/event-stream")


@ui.post("/t/{tenant_id}/runs/{thread_id}/stop")
def stop(
    thread_id: str, page: Tenant, csrf: Annotated[str, Form()], reason: Annotated[str, Form()] = ""
) -> RedirectResponse:
    page.check(csrf)
    members.require(page.db, page.who, "reviewer")
    back = f"/ui/t/{page.db.tenant.tenant_id}/runs/{thread_id}"
    row = threads.get(page.db, thread_id)
    if row is None or row.status not in LIVE:
        return _back(back, "the run is not running")
    asked = stops.request(page.db, thread_id, page.who.principal_id, reason or None)
    return _back(back, "stop requested: it ends at its next step" if asked else "already asked")
