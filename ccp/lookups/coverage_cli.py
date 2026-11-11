"""`ccp coverage ...`: the inventory, the coverage map, class approvals and the registry
diff (spec.md A-166)."""

from __future__ import annotations

import json
from typing import Any

import typer

from ccp.config import Settings
from ccp.knowledge import service_classes
from ccp.lookups import coverage, inventory, playbooks
from ccp.observability import steps
from ccp.storage import db
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

app = typer.Typer(no_args_is_help=True, help="Coverage of the cloud inventory, measured")


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


@app.command("inventory")
def take(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        found = inventory.take(scoped, settings)
        typer.echo(json.dumps(found.__dict__, indent=2))
    finally:
        pool.close()


@app.command("map")
def show(
    tenant: str | None = typer.Option(None, "--tenant"),
    rows: bool = typer.Option(False, "--rows", help="every resource and intent"),
) -> None:
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        built = coverage.build(scoped)
        typer.echo(json.dumps(coverage.summary(built), indent=2))
        if rows:
            typer.echo(json.dumps(coverage.as_dicts(built), indent=2, default=str))
    finally:
        pool.close()


@app.command("classify")
def classify(
    resource_type: str = typer.Option(..., "--type"),
    cls: str | None = typer.Option(None, "--class", help="omit to see the proposal"),
    by: str = typer.Option("", "--by"),
) -> None:
    """Show the proposed class for a new type, or approve one as a named person."""
    if cls is None:
        typer.echo(f"{resource_type}: proposed {service_classes.propose_class(resource_type)}")
        return
    try:
        typer.echo(json.dumps(service_classes.approve(resource_type, cls, by), indent=2))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("registry")
def registry() -> None:
    """Compare AWS's resource-type registry with the last list kept."""
    typer.echo(json.dumps(inventory.registry_diff(_settings())))


@app.command("playbooks")
def run_playbooks(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Read today's gaps with their class's playbook: fail or needs_review, never pass."""
    settings = _settings()
    scoped, pool = _scoped(settings, tenant)
    try:
        typer.echo(json.dumps(playbooks.run(scoped, settings).__dict__, indent=2))
    finally:
        pool.close()
