"""`ccp evidence ...`: feedback on uploads, auditors' labels, and the adequacy verifier's
calibration (spec.md A-161, A-162)."""

from __future__ import annotations

import json
from typing import Any, Literal

import typer

from ccp.config import Settings
from ccp.evaluators import adequacy, feedback
from ccp.observability import steps
from ccp.storage import db
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

app = typer.Typer(no_args_is_help=True, help="Uploads: feedback, auditor labels, the verifier")


def _settings() -> Settings:
    settings = Settings()
    steps.configure(settings)
    return settings


def _scoped(settings: Settings, tenant_id: str | None) -> tuple[ScopedDB, Any]:
    chosen = tenant_id or settings.ccp_tenant
    if not chosen:
        raise typer.BadParameter("pass --tenant or set CCP_TENANT")
    pool = db.app_pool(settings)
    return ScopedDB(pool, TenantContext.for_id(settings, chosen)), pool


@app.command("feedback")
def show_feedback(
    entity: str = typer.Option(..., "--entity"),
    shadow: bool = typer.Option(False, "--shadow", help="also show answers held in shadow"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        feedback.drain(scoped, settings)
        typer.echo(json.dumps(feedback.latest(scoped, entity, not shadow), default=str, indent=2))
    finally:
        pool.close()


@app.command("verify")
def verify(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Run the adequacy jobs that are queued (they park without a model key)."""
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        feedback.drain(scoped, settings)
        typer.echo(f"verified {adequacy.drain(scoped, settings)}")
    finally:
        pool.close()


@app.command("label")
def label(
    entity: str = typer.Option(..., "--entity"),
    accept: bool = typer.Option(False, "--accept"),
    flag: bool = typer.Option(False, "--flag"),
    by: str = typer.Option(..., "--by"),
    note: str = typer.Option("", "--note"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """An auditor's accept-or-flag decision on the newest version of a document."""
    if accept == flag:
        raise typer.BadParameter("pass exactly one of --accept and --flag")
    decision: Literal["accept", "flag"] = "accept" if accept else "flag"
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        typer.echo(adequacy.label(scoped, entity, decision, by, note))
    finally:
        pool.close()


@app.command("calibrate")
def calibrate(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """The verifier against auditors; exit 1 while it may not be shown."""
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        result = adequacy.calibrate(scoped, settings)
        typer.echo(json.dumps(result.as_dict(), indent=2))
        if not result.passed:
            raise typer.Exit(1)
    finally:
        pool.close()
