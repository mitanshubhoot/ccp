"""`ccp lookups ...`: run the reads about failing resources, and show what they found
(spec.md A-158)."""

from __future__ import annotations

import json
from typing import Any

import typer

from ccp.config import Settings
from ccp.lookups import runner, store
from ccp.observability import steps
from ccp.storage import db
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

app = typer.Typer(no_args_is_help=True, help="Reads about failing resources, before investigation")


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


@app.command("run")
def run(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Queue and run the lookups for every new failing verdict with a plan."""
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        summary = runner.drain(scoped, settings)
        typer.echo(
            f"queued {summary.enqueued}, looked up {summary.looked_up}, landed"
            f" {summary.landed} responses, queued {summary.investigations} investigations"
        )
    finally:
        pool.close()


@app.command("show")
def show(
    verdict: str = typer.Option(..., "--verdict"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """What the lookups found about one verdict's failing resources."""
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        found = store.for_verdict(scoped, verdict)
        if not found:
            typer.echo("no lookups for this verdict")
        for lookup in found:
            typer.echo(json.dumps(store.summary(lookup), default=str, indent=2))
    finally:
        pool.close()
