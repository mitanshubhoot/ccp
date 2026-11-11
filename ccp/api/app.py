"""The routes (spec.md A-134).

    GET    /v1/health                                   no sign-in
    GET    /v1/me                                       who you are, and your tenants
    POST   /v1/tenants                                  onboard a tenant; you become its admin
    GET    /v1/tenants/{t}/approvals[?open=&kind=]      the inbox (viewer)
    GET    /v1/tenants/{t}/approvals/{id}               one item: payload, hash, expiry, history
    POST   /v1/tenants/{t}/approvals/{id}/approve       with the payload hash you were shown
    POST   /v1/tenants/{t}/approvals/{id}/reject        with that hash and a note
    POST   /v1/tenants/{t}/approvals/{id}/request-changes
    POST   /v1/tenants/{t}/approvals/{id}/break-glass   an admin, with the hash and a reason
    GET    /v1/tenants/{t}/findings                     current status per subject (viewer)
    GET    /v1/tenants/{t}/report/{framework}           the framework rollup (viewer)
    GET    /v1/tenants/{t}/proposals[/{id}]             remediation proposals (viewer)
    POST   /v1/tenants/{t}/proposals/{id}/dry-run       the exact call, recorded (reviewer)
    GET    /v1/tenants/{t}/exceptions                   (viewer)
    POST   /v1/tenants/{t}/exceptions                   ask for one (reviewer)
    POST   /v1/tenants/{t}/exceptions/{id}/revoke       (approver)
    GET    /v1/tenants/{t}/members                      (admin)
    PUT    /v1/tenants/{t}/members                      grant a role (admin)
    DELETE /v1/tenants/{t}/members?issuer=&subject=     end a membership (admin)
    GET    /v1/tenants/{t}/settings                     the version in force (viewer)
    PATCH  /v1/tenants/{t}/settings                     a change; scope waits for an approver
    GET    /v1/tenants/{t}/tools                        the tool surface (A-117), as a reader
    POST   /v1/tenants/{t}/tools/{name}

A request without a valid token is 401. A tenant the caller is not a member of is 404, the
same as one that does not exist, so membership is not disclosed. A role too low is 403.
A decision on a changed payload, an expired item, or one decided already is 409; a request
the services refuse as malformed is 422. Live remediation stays on the operator's command
line behind its five gates (A-131); the API runs dry runs only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from ccp import auth, tenancy
from ccp.agent import rest
from ccp.agent.rest import Caller
from ccp.api import ui
from ccp.approvals import actions, store
from ccp.auth import members
from ccp.compliance_state import exceptions as exception_requests
from ccp.compliance_state import reader
from ccp.config import Settings
from ccp.evaluators.tier2 import Providers
from ccp.knowledge import graph as knowledge
from ccp.remediation import proposals, runner
from ccp.storage import db as storage
from ccp.storage import exceptions as stored_exceptions
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from ccp.tenants import onboarding, scope, versions


@dataclass
class Pools:
    app: ConnectionPool
    agent: ConnectionPool | None = None


@dataclass(frozen=True)
class Session:
    """A signed-in member acting in one tenant."""

    settings: Settings
    db: ScopedDB
    who: auth.Principal
    role: members.Role
    pools: Pools


# -- request bodies ------------------------------------------------------------------------


class Decide(BaseModel):
    payload_sha256: str = Field(min_length=64, max_length=64)
    note: str | None = None
    decision: Literal["approve", "override", "regather"] = "approve"
    status: str | None = None


class Reject(BaseModel):
    payload_sha256: str = Field(min_length=64, max_length=64)
    note: str = Field(min_length=1)


class Note(BaseModel):
    note: str = Field(min_length=1)


class BreakGlass(BaseModel):
    payload_sha256: str = Field(min_length=64, max_length=64)
    reason: str


class NewTenant(BaseModel):
    tenant_id: str
    display_name: str
    github_org: str
    time_zone: str = "UTC"


class NewException(BaseModel):
    test_id: str
    subject: str
    until: datetime
    reason: str
    owner: str


class Revoke(BaseModel):
    reason: str


class Grant(BaseModel):
    issuer: str
    subject: str
    role: members.Role
    email: str | None = None
    name: str | None = None


class SettingsChange(BaseModel):
    changes: dict[str, Any]


# -- errors --------------------------------------------------------------------------------

ERRORS: tuple[tuple[type[Exception], int], ...] = (
    (auth.InvalidToken, 401),
    (members.NotPermitted, 403),
    (PermissionError, 403),
    (LookupError, 404),
    (store.Expired, 409),
    (store.AlreadyDecided, 409),
    (actions.PayloadChanged, 409),
    (scope.Superseded, 409),
    (onboarding.AlreadyExists, 409),
    (actions.InvalidDecision, 422),
    (exception_requests.NotExceptable, 422),
    (ValueError, 422),
)


def _error(status: int) -> Callable[[Request, Exception], Coroutine[Any, Any, JSONResponse]]:
    async def handle(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": str(exc)})

    return handle


# -- who is asking, and in which tenant ----------------------------------------------------


def _settings(request: Request) -> Settings:
    found: Settings = request.app.state.settings
    return found


def _pools(request: Request) -> Pools:
    found: Pools = request.app.state.pools
    return found


def principal(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> auth.Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "sign in: send Authorization: Bearer <token>")
    try:
        return auth.authenticate(_settings(request), authorization.split(" ", 1)[1].strip())
    except auth.InvalidToken as exc:
        raise HTTPException(401, f"sign-in token refused: {exc}") from exc


Who = Annotated[auth.Principal, Depends(principal)]


def session(request: Request, tenant_id: str, who: Who) -> Session:
    settings = _settings(request)
    try:
        ctx = TenantContext.for_id(settings, tenant_id)
    except (tenancy.UnknownTenant, tenancy.TenantUnavailable) as exc:
        raise HTTPException(404, f"no tenant {tenant_id!r}") from exc
    db = ScopedDB(_pools(request).app, ctx)
    role = members.role_of(db, who)
    if role is None:  # the same answer as a tenant that does not exist
        raise HTTPException(404, f"no tenant {tenant_id!r}")
    return Session(settings, db, who, role, _pools(request))


Member = Annotated[Session, Depends(session)]


def _at_least(s: Session, role: members.Role) -> None:
    members.require(s.db, s.who, role)


def _item(found: store.Approval) -> dict[str, Any]:
    out: dict[str, Any] = jsonable_encoder(asdict(found))
    return out


def _decide(fn: Callable[[], actions.Outcome]) -> dict[str, Any]:
    try:
        outcome = fn()
    except ValueError as exc:
        if "is already" in str(exc):  # decided before this request reached it
            raise HTTPException(409, str(exc)) from exc
        raise
    return {"approval": _item(outcome.approval), "detail": outcome.detail, "ok": outcome.ok}


router = APIRouter(prefix="/v1")


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    return {"ok": True, "auth_mode": _settings(request).ccp_auth_mode}


@router.get("/me")
def me(request: Request, who: Who) -> dict[str, Any]:
    tenants = []
    for ctx in TenantContext.all(_settings(request)):
        role = members.role_of(ScopedDB(_pools(request).app, ctx), who)
        if role is not None:
            tenants.append(
                {"tenant_id": ctx.tenant_id, "name": ctx.settings.display_name, "role": role}
            )
    return {"principal_id": who.principal_id, "label": who.label, "tenants": tenants}


@router.post("/tenants", status_code=201)
def onboard(request: Request, body: NewTenant, who: Who) -> dict[str, Any]:
    ctx = onboarding.create(
        _settings(request),
        _pools(request).app,
        body.tenant_id,
        display_name=body.display_name,
        github_org=body.github_org,
        admin=who,
        recorded_by=who.principal_id,
        time_zone=body.time_zone,
    )
    return {"tenant_id": ctx.tenant_id, "admin": who.principal_id}


# -- approvals -----------------------------------------------------------------------------


@router.get("/tenants/{tenant_id}/approvals")
def inbox(s: Member, open: bool = True, kind: str | None = None) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "items": [
            {**_item(i), "expired": i.expired(now)}
            for i in store.list_items(s.db, kind, open_only=open)
            if not (open and i.expired(now))  # an expired item acts on nothing
        ]
    }


@router.get("/tenants/{tenant_id}/approvals/{approval_id}")
def item(approval_id: str, s: Member) -> dict[str, Any]:
    found = store.get(s.db, approval_id)
    if found is None:
        raise HTTPException(404, f"no approval {approval_id!r}")
    return {**_item(found), "history": store.history(s.db, approval_id)}


@router.post("/tenants/{tenant_id}/approvals/{approval_id}/approve")
def approve(approval_id: str, body: Decide, s: Member) -> dict[str, Any]:
    return _decide(
        lambda: actions.approve(
            s.db,
            s.settings,
            approval_id,
            s.who,
            body.note,
            decision=body.decision,
            status=body.status,
            seen_sha256=body.payload_sha256,
        )
    )


@router.post("/tenants/{tenant_id}/approvals/{approval_id}/reject")
def reject(approval_id: str, body: Reject, s: Member) -> dict[str, Any]:
    return _decide(
        lambda: actions.reject(
            s.db, s.settings, approval_id, s.who, body.note, seen_sha256=body.payload_sha256
        )
    )


@router.post("/tenants/{tenant_id}/approvals/{approval_id}/request-changes")
def request_changes(approval_id: str, body: Note, s: Member) -> dict[str, Any]:
    return _decide(lambda: actions.request_changes(s.db, approval_id, s.who, body.note))


@router.post("/tenants/{tenant_id}/approvals/{approval_id}/break-glass")
def break_glass(approval_id: str, body: BreakGlass, s: Member) -> dict[str, Any]:
    return _decide(
        lambda: actions.break_glass(
            s.db, s.settings, approval_id, s.who, body.reason, seen_sha256=body.payload_sha256
        )
    )


# -- what is true --------------------------------------------------------------------------


@router.get("/tenants/{tenant_id}/findings")
def findings(s: Member) -> dict[str, Any]:
    held = stored_exceptions.excepted_until(s.db)
    rows = [
        {
            "subject_key": r.subject_key,
            "test_id": r.test_id,
            "status": r.status.value,
            "shown_as": stored_exceptions.label(r.status.value, held.get(r.subject_key)),
            "verdict_id": r.verdict_id,
            "valid_from": r.valid_from,
        }
        for r in reader.heads(s.db)
    ]
    return {"findings": jsonable_encoder(rows)}


@router.get("/tenants/{tenant_id}/report/{framework_id}")
def report(framework_id: str, s: Member) -> dict[str, Any]:
    if framework_id not in knowledge.load().frameworks:
        raise HTTPException(404, f"no framework {framework_id!r}")
    result = reader.framework_rollup(s.db, framework_id)
    rows = [
        {
            "requirement_id": r.requirement_id,
            "state": r.state,
            "shown_as": r.shown,
            "excepted_until": r.excepted_until,
            "blocking_test_id": r.blocking_test_id,
            "blocking_subject_key": r.blocking_subject_key,
            "pending_reasons": r.pending_reasons,
        }
        for r in result.requirements
    ]
    return {
        "framework_id": framework_id,
        "no_evaluations_yet": result.no_evaluations_yet,
        "summary": result.summary,
        "requirements": jsonable_encoder(rows),
    }


# -- remediation ---------------------------------------------------------------------------


@router.get("/tenants/{tenant_id}/proposals")
def proposal_list(s: Member, status: str | None = None) -> dict[str, Any]:
    org = s.db.tenant.settings.github_org
    found = [runner.describe(s.db, p, org) for p in proposals.list_all(s.db, status)]
    return {"proposals": jsonable_encoder(found)}


@router.get("/tenants/{tenant_id}/proposals/{proposal_id}")
def proposal(proposal_id: str, s: Member) -> dict[str, Any]:
    found = proposals.get(s.db, proposal_id)
    if found is None:
        raise HTTPException(404, f"no proposal {proposal_id!r}")
    described: dict[str, Any] = jsonable_encoder(
        runner.describe(s.db, found, s.db.tenant.settings.github_org)
    )
    return described


@router.post("/tenants/{tenant_id}/proposals/{proposal_id}/dry-run")
def dry_run(proposal_id: str, s: Member) -> dict[str, Any]:
    _at_least(s, "reviewer")
    result = runner.execute(s.db, s.settings, proposal_id, live=False)
    return {
        "mode": result.mode,
        "detail": result.detail,
        "planned_call": result.proposal.planned_call,
    }


# -- exceptions ----------------------------------------------------------------------------


@router.get("/tenants/{tenant_id}/exceptions")
def exception_list(s: Member) -> dict[str, Any]:
    now = datetime.now(UTC)
    found = [{**asdict(e), "holds": e.holds(now)} for e in stored_exceptions.all_exceptions(s.db)]
    return {"exceptions": jsonable_encoder(found)}


@router.post("/tenants/{tenant_id}/exceptions", status_code=202)
def exception_ask(body: NewException, s: Member) -> dict[str, Any]:
    item_id = exception_requests.request(
        s.db,
        s.who,
        test_id=body.test_id,
        subject=body.subject,
        until=body.until,
        reason=body.reason,
        owner=body.owner,
    )
    return {"approval_id": item_id, "detail": "waits for an approver"}


@router.post("/tenants/{tenant_id}/exceptions/{exception_id}/revoke")
def exception_revoke(exception_id: str, body: Revoke, s: Member) -> dict[str, Any]:
    exception_requests.revoke(s.db, s.who, exception_id, body.reason)
    return {"exception_id": exception_id, "revoked_by": s.who.principal_id}


# -- members and settings ------------------------------------------------------------------


def _keep_an_admin(s: Session, leaving: auth.Principal) -> None:
    others = [
        m
        for m in members.current(s.db)
        if m.role == "admin" and m.principal.principal_id != leaving.principal_id
    ]
    if not others:
        raise HTTPException(409, "a tenant keeps at least one admin")


@router.get("/tenants/{tenant_id}/members")
def member_list(s: Member) -> dict[str, Any]:
    _at_least(s, "admin")
    return {"members": jsonable_encoder([asdict(m) for m in members.current(s.db)])}


@router.put("/tenants/{tenant_id}/members")
def member_grant(body: Grant, s: Member) -> dict[str, Any]:
    _at_least(s, "admin")
    target = auth.Principal(body.issuer, body.subject, body.email, body.name)
    if members.role_of(s.db, target) == "admin" and body.role != "admin":
        _keep_an_admin(s, target)
    granted = members.grant(s.db, target, body.role, s.who.principal_id)
    return {"member": jsonable_encoder(asdict(granted))}


@router.delete("/tenants/{tenant_id}/members")
def member_revoke(issuer: str, subject: str, s: Member) -> dict[str, Any]:
    _at_least(s, "admin")
    target = auth.Principal(issuer, subject)
    if members.role_of(s.db, target) == "admin":
        _keep_an_admin(s, target)
    ended = members.revoke(s.db, target, s.who.principal_id)
    return {"member": jsonable_encoder(asdict(ended))}


@router.get("/tenants/{tenant_id}/settings")
def settings_show(s: Member) -> dict[str, Any]:
    current, version = versions.effective(s.db)
    return {"version": version, "settings": current.model_dump(mode="json")}


@router.patch("/tenants/{tenant_id}/settings")
def settings_change(body: SettingsChange, s: Member) -> JSONResponse:
    version, item_id = scope.change(s.db, s.who, body.changes)
    if item_id is not None:
        return JSONResponse(
            status_code=202,
            content={"approval_id": item_id, "detail": "a change of scope waits for an approver"},
        )
    return JSONResponse(status_code=200, content={"version": version})


# -- the tool surface (A-117) --------------------------------------------------------------


def _tools(s: Session, method: str, path: str, body: bytes | None) -> JSONResponse:
    agent = s.pools.agent
    if agent is None:
        raise HTTPException(503, "the read-only tool role is not configured")
    status, out = rest.handle(
        Caller("reader", s.who.principal_id),
        method,
        path,
        body,
        log_db=s.db,
        read_db=ScopedDB(agent, s.db.tenant),
        providers=Providers.from_settings(s.settings),
    )
    return JSONResponse(status_code=status, content=jsonable_encoder(out))


@router.get("/tenants/{tenant_id}/tools")
def tools(s: Member) -> JSONResponse:
    return _tools(s, "GET", "/v1/tools", None)


@router.post("/tenants/{tenant_id}/tools/{name}")
async def tool_call(name: str, request: Request, s: Member) -> JSONResponse:
    return _tools(s, "POST", f"/v1/tools/{name}", await request.body())


# -- the app -------------------------------------------------------------------------------


def create_app(
    settings: Settings, pools: Pools | None = None, http: httpx.Client | None = None
) -> FastAPI:
    """The API over `settings`. Pass `pools` to share connections (tests); otherwise the app
    opens its own when it starts and closes them when it stops."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if pools is not None:
            app.state.pools = pools
            yield
            return
        own = Pools(storage.app_pool(settings), storage.agent_pool(settings))
        app.state.pools = own
        try:
            yield
        finally:
            own.app.close()
            if own.agent is not None:
                own.agent.close()

    app = FastAPI(title="CCP", version="1", lifespan=lifespan)
    app.state.settings = settings
    app.state.http = http or httpx.Client()
    if pools is not None:
        app.state.pools = pools
    for kind, status in ERRORS:
        app.add_exception_handler(kind, _error(status))
    app.add_exception_handler(ui.SignIn, _to_sign_in)
    app.include_router(router)
    app.include_router(ui.ui)
    return app


async def _to_sign_in(request: Request, exc: Exception) -> RedirectResponse:
    return RedirectResponse("/ui/login", status_code=303)
