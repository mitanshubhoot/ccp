"""The CLI: ``python -m ccp ...`` or ``ccp ...`` after ``uv sync``."""

from __future__ import annotations

from datetime import datetime

import typer

from ccp.config import Settings
from ccp.connectors.base import parse_ts
from ccp.connectors.registry import CONNECTOR_NAMES, SOURCE_KINDS, build_connector
from ccp.observability import steps
from ccp.storage import db
from ccp.tenancy import TenantContext

app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="ccp: compliance companion platform"
)
tenants_app = typer.Typer(no_args_is_help=True, help="Tenants from config/tenants.yaml")
db_app = typer.Typer(no_args_is_help=True, help="Database migrations and checks")
connectors_app = typer.Typer(no_args_is_help=True, help="Pull provider data as SourceEvents")
app.add_typer(tenants_app, name="tenants")
app.add_typer(db_app, name="db")
app.add_typer(connectors_app, name="connectors")


def _settings() -> Settings:
    settings = Settings()
    steps.configure(settings)
    return settings


def _tenant(settings: Settings, tenant_id: str | None) -> TenantContext:
    chosen = tenant_id or settings.ccp_tenant
    if not chosen:
        raise typer.BadParameter("pass --tenant or set CCP_TENANT")
    return TenantContext.for_id(settings, chosen)


@connectors_app.command("pull")
def connectors_pull(
    connector: str = typer.Option(..., "--connector", help="github | hris | manual"),
    tenant: str | None = typer.Option(None, "--tenant"),
    source: str = typer.Option("fixture", "--source", help="fixture | live"),
    since: str | None = typer.Option(None, "--since", help="ISO timestamp; only newer events"),
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run", help="Print events; do not land them"
    ),
) -> None:
    """Pull one connector for one tenant and print each SourceEvent on a line."""
    name = next((n for n in CONNECTOR_NAMES if n == connector), None)
    if name is None:
        raise typer.BadParameter(f"connector must be one of {', '.join(CONNECTOR_NAMES)}")
    kind = next((k for k in SOURCE_KINDS if k == source), None)
    if kind is None:
        raise typer.BadParameter("source must be fixture or live")
    settings = _settings()
    ctx = _tenant(settings, tenant)
    conn = build_connector(name, settings, kind)
    since_ts: datetime | None = parse_ts(since) if since else None
    if not dry_run:
        typer.echo("landing events is Phase 2 (`ccp ingest run`); printing only", err=True)
    count = 0
    with steps.step(
        "connectors.pull",
        layer="connectors",
        tenant=ctx,
        inputs={"connector": connector, "source": source},
    ) as s:
        for event in conn.pull(ctx, since_ts):
            count += 1
            extra = ""
            pdf = event.payload.get("pdf")
            if isinstance(pdf, dict):
                extra = f" pages={pdf.get('page_count')} status={pdf.get('status')}"
            elif "text" in event.payload:
                extra = f" chars={len(str(event.payload['text']))}"
            stamp = event.source_ts.isoformat()
            typer.echo(f"{event.event_type:<34} {event.source_event_id:<52} {stamp}{extra}")
        s.set_output({"events": count})
    typer.echo(f"{count} events", err=True)


@tenants_app.command("list")
def tenants_list() -> None:
    """List the configured tenants."""
    for ctx in TenantContext.all(_settings()):
        s = ctx.settings
        typer.echo(
            f"{ctx.tenant_id:<8} {s.display_name:<20} github_org={s.github_org} "
            f"admin_limit={s.org_admin_limit} sla_h={s.termination_revocation_sla_hours} "
            f"critical_tags={','.join(s.critical_asset_tags)}"
        )


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending migrations as the admin role and ensure the app role exists."""
    settings = _settings()
    with steps.step("db.migrate", layer="storage") as s:
        applied = db.migrate(settings)
        s.set_output({"applied": applied})
    if applied:
        for name in applied:
            typer.echo(f"applied {name}")
    else:
        typer.echo("nothing to apply")


@db_app.command("check")
def db_check() -> None:
    """Connect as the application role and print what the server looks like."""
    for key, value in db.check(_settings()).items():
        typer.echo(f"{key}: {value}")
