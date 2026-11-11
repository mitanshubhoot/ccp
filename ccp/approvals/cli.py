"""``ccp approvals ...``: list, show, approve, reject, request-changes, close, resume, audit.

A refused decision exits 2 and records nothing; a decision that was recorded but could not be
applied exits 1 and names the `resume` command that applies it (A-95). A decision proves who
makes it with a sign-in token (`--token`, or CCP_TOKEN), never a typed name (A-130)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import NoReturn

import typer

from ccp import auth
from ccp.agent import decisions
from ccp.approvals import actions, store
from ccp.compliance_state import exceptions
from ccp.config import Settings
from ccp.observability import steps
from ccp.storage import db
from ccp.storage import exceptions as stored_exceptions
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

app = typer.Typer(no_args_is_help=True, help="The shared human-in-the-loop stage")
exceptions_app = typer.Typer(
    no_args_is_help=True, help="Control exceptions: a failure accepted until a date (A-132)"
)


def _settings() -> Settings:
    settings = Settings()
    steps.configure(settings)
    return settings


def _tenant(settings: Settings, tenant_id: str | None) -> TenantContext:
    chosen = tenant_id or settings.ccp_tenant
    if not chosen:
        raise typer.BadParameter("pass --tenant or set CCP_TENANT")
    return TenantContext.for_id(settings, chosen)


def _refuse(message: str) -> NoReturn:
    typer.echo(f"refused: {message}", err=True)
    raise typer.Exit(2)


REFUSED = (LookupError, ValueError, PermissionError, actions.PayloadChanged)
TOKEN = typer.Option(..., "--token", envvar="CCP_TOKEN", help="your sign-in token")


def _signed_in(settings: Settings, token: str) -> auth.Principal:
    try:
        return auth.authenticate(settings, token)
    except auth.InvalidToken as exc:
        _refuse(f"sign-in token refused: {exc}")


def _report(approval_id: str, reviewer: str, outcome: actions.Outcome) -> None:
    typer.echo(f"{approval_id}: {outcome.approval.state} by {reviewer}; {outcome.detail}")
    if not outcome.ok:
        raise typer.Exit(1)


@app.command("list")
def list_cmd(
    tenant: str | None = typer.Option(None, "--tenant"),
    kind: str | None = typer.Option(None, "--kind"),
    open_only: bool = typer.Option(False, "--open", help="only items awaiting a decision"),
) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        items = store.list_items(ScopedDB(pool, ctx), kind, open_only)
    finally:
        pool.close()
    if not items:
        typer.echo("no approval items")
    for item in items:
        typer.echo(f"{item.approval_id}  {item.kind:<20} {item.state:<18} {item.ref_id}")


@app.command("show")
def show(approval_id: str, tenant: str | None = typer.Option(None, "--tenant")) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        item = store.get(scoped, approval_id)
        events = store.history(scoped, approval_id) if item else []
    finally:
        pool.close()
    if item is None:
        raise typer.BadParameter(f"no approval item {approval_id!r}")
    typer.echo(
        json.dumps(
            {
                "approval_id": item.approval_id,
                "kind": item.kind,
                "ref_id": item.ref_id,
                "state": item.state,
                "reviewer": item.reviewer,
                "note": item.note,
                "decision": item.decision,
                "payload_sha256": item.payload_sha256,
                "payload": item.payload,
                "events": events,
            },
            indent=2,
            default=str,
        )
    )


@app.command("approve")
def approve(
    approval_id: str,
    token: str = TOKEN,
    note: str | None = typer.Option(None, "--note"),
    decision: str = typer.Option(
        "approve", "--decision", help="tier3_interrupt: approve | override | regather"
    ),
    status: str | None = typer.Option(None, "--status", help="with --decision override"),
    seen: str | None = typer.Option(
        None, "--payload-sha256", help="the payload hash you were shown (`approvals show`)"
    ),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    who = _signed_in(settings, token)
    pool = db.app_pool(settings)
    try:
        outcome = actions.approve(
            ScopedDB(pool, ctx),
            settings,
            approval_id,
            who,
            note,
            decision=decision,
            status=status,
            seen_sha256=seen,
        )
    except REFUSED as exc:
        _refuse(str(exc))
    finally:
        pool.close()
    _report(approval_id, who.label, outcome)


@app.command("reject")
def reject(
    approval_id: str,
    token: str = TOKEN,
    note: str = typer.Option(..., "--note"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    who = _signed_in(settings, token)
    pool = db.app_pool(settings)
    try:
        outcome = actions.reject(ScopedDB(pool, ctx), settings, approval_id, who, note)
    except REFUSED as exc:
        _refuse(str(exc))
    finally:
        pool.close()
    _report(approval_id, who.label, outcome)


@app.command("resume")
def resume(approval_id: str, tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Apply a recorded tier 3 decision whose resume failed."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        outcome = actions.resume(ScopedDB(pool, ctx), settings, approval_id)
    except (LookupError, ValueError) as exc:
        _refuse(str(exc))
    finally:
        pool.close()
    _report(approval_id, outcome.approval.reviewer, outcome)


@app.command("request-changes")
def request_changes(
    approval_id: str,
    token: str = TOKEN,
    note: str = typer.Option(..., "--note"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    who = _signed_in(settings, token)
    pool = db.app_pool(settings)
    try:
        outcome = actions.request_changes(ScopedDB(pool, ctx), approval_id, who, note)
    except REFUSED as exc:
        _refuse(str(exc))
    finally:
        pool.close()
    typer.echo(f"{approval_id}: {outcome.approval.state}")


@app.command("close")
def close(
    approval_id: str,
    token: str = TOKEN,
    note: str | None = typer.Option(None, "--note"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    who = _signed_in(settings, token)
    pool = db.app_pool(settings)
    try:
        outcome = actions.close(ScopedDB(pool, ctx), approval_id, who, note)
    except REFUSED as exc:
        _refuse(str(exc))
    finally:
        pool.close()
    typer.echo(f"{approval_id}: {outcome.approval.state}")


@app.command("audit")
def audit(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Tier 3 decisions recorded but not applied, and applied without a record. Exits 1 on
    any problem."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        pending = decisions.unapplied(scoped)
        problems = decisions.audit(scoped)
        glass = store.break_glass_problems(scoped)
    finally:
        pool.close()
    for item in pending:
        typer.echo(
            f"unapplied: {item.approval_id} ({item.state}) for thread {item.ref_id};"
            f" run `ccp approvals resume {item.approval_id}`"
        )
    for problem in problems:
        typer.echo(f"unrecorded: {problem}")
    for problem in glass:
        typer.echo(f"break-glass: {problem}")
    if pending or problems or glass:
        raise typer.Exit(1)
    typer.echo(
        "every applied tier 3 decision is recorded, every recorded one is applied, and every"
        " break-glass use is reviewed or still within its review window"
    )


@app.command("break-glass")
def break_glass(
    approval_id: str,
    reason: str = typer.Option(..., "--reason", help="why this cannot wait for another approver"),
    token: str = TOKEN,
    seen: str | None = typer.Option(None, "--payload-sha256"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """An admin approves an urgent item they asked for; another approver reviews the use."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    who = _signed_in(settings, token)
    pool = db.app_pool(settings)
    try:
        outcome = actions.break_glass(
            ScopedDB(pool, ctx), settings, approval_id, who, reason, seen_sha256=seen
        )
    except REFUSED as exc:
        _refuse(str(exc))
    finally:
        pool.close()
    _report(approval_id, who.label, outcome)


@exceptions_app.command("request")
def exception_request(
    test: str = typer.Option(..., "--test"),
    subject: str = typer.Option(..., "--subject"),
    until: str = typer.Option(..., "--until", help="ISO date the exception ends"),
    reason: str = typer.Option(..., "--reason"),
    owner: str = typer.Option(..., "--owner", help="who answers for it"),
    token: str = TOKEN,
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Ask for an exception for one failing subject; another member, an approver, decides."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    who = _signed_in(settings, token)
    ends = datetime.fromisoformat(until)
    pool = db.app_pool(settings)
    try:
        item = exceptions.request(
            ScopedDB(pool, ctx),
            who,
            test_id=test,
            subject=subject,
            until=ends if ends.tzinfo else ends.replace(tzinfo=UTC),
            reason=reason,
            owner=owner,
        )
    except REFUSED as exc:
        _refuse(str(exc))
    finally:
        pool.close()
    typer.echo(f"exception requested: approval {item} waits for an approver")


@exceptions_app.command("list")
def exception_list(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        found = stored_exceptions.all_exceptions(ScopedDB(pool, ctx))
    finally:
        pool.close()
    if not found:
        typer.echo("no exceptions")
    now = datetime.now(UTC)
    for e in found:
        state = "holds" if e.holds(now) else ("revoked" if e.revoked_at else "lapsed")
        typer.echo(
            f"{e.exception_id}  {state:<8} {e.subject_key}  until {e.valid_until:%Y-%m-%d}"
            f"  owner={e.owner}  {e.reason}"
        )


@exceptions_app.command("revoke")
def exception_revoke(
    exception_id: str,
    reason: str = typer.Option(..., "--reason"),
    token: str = TOKEN,
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """End an exception early (an approver)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    who = _signed_in(settings, token)
    pool = db.app_pool(settings)
    try:
        exceptions.revoke(ScopedDB(pool, ctx), who, exception_id, reason)
    except REFUSED as exc:
        _refuse(str(exc))
    finally:
        pool.close()
    typer.echo(f"{exception_id}: revoked by {who.label}")
