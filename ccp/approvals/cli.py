"""``ccp approvals ...``: list, show, approve, reject, request-changes, close."""

from __future__ import annotations

import json

import typer

from ccp.approvals import actions, store
from ccp.config import Settings
from ccp.observability import steps
from ccp.storage import db
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

app = typer.Typer(no_args_is_help=True, help="The shared human-in-the-loop stage")


def _settings() -> Settings:
    settings = Settings()
    steps.configure(settings)
    return settings


def _tenant(settings: Settings, tenant_id: str | None) -> TenantContext:
    chosen = tenant_id or settings.ccp_tenant
    if not chosen:
        raise typer.BadParameter("pass --tenant or set CCP_TENANT")
    return TenantContext.for_id(settings, chosen)


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
        typer.echo(f"{item.approval_id}  {item.kind:<17} {item.state:<18} {item.ref_id}")


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
    reviewer: str = typer.Option(..., "--reviewer"),
    note: str | None = typer.Option(None, "--note"),
    decision: str = typer.Option(
        "approve", "--decision", help="tier3_interrupt: approve | override | regather"
    ),
    status: str | None = typer.Option(None, "--status", help="with --decision override"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        outcome = actions.approve(
            ScopedDB(pool, ctx),
            settings,
            approval_id,
            reviewer,
            note,
            decision=decision,
            status=status,
        )
    finally:
        pool.close()
    typer.echo(f"{approval_id}: {outcome.approval.state} by {reviewer}; {outcome.detail}")


@app.command("reject")
def reject(
    approval_id: str,
    reviewer: str = typer.Option(..., "--reviewer"),
    note: str = typer.Option(..., "--note"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        outcome = actions.reject(ScopedDB(pool, ctx), settings, approval_id, reviewer, note)
    finally:
        pool.close()
    typer.echo(f"{approval_id}: {outcome.approval.state} by {reviewer}; {outcome.detail}")


@app.command("request-changes")
def request_changes(
    approval_id: str,
    reviewer: str = typer.Option(..., "--reviewer"),
    note: str = typer.Option(..., "--note"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        outcome = actions.request_changes(ScopedDB(pool, ctx), approval_id, reviewer, note)
    finally:
        pool.close()
    typer.echo(f"{approval_id}: {outcome.approval.state}")


@app.command("close")
def close(
    approval_id: str,
    reviewer: str = typer.Option(..., "--reviewer"),
    note: str | None = typer.Option(None, "--note"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        outcome = actions.close(ScopedDB(pool, ctx), approval_id, reviewer, note)
    finally:
        pool.close()
    typer.echo(f"{approval_id}: {outcome.approval.state}")
