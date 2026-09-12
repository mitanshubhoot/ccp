"""The CLI: ``python -m ccp ...`` or ``ccp ...`` after ``uv sync``."""

from __future__ import annotations

import typer

from ccp.config import Settings
from ccp.observability import steps
from ccp.storage import db
from ccp.tenancy import TenantContext

app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="ccp: compliance companion platform"
)
tenants_app = typer.Typer(no_args_is_help=True, help="Tenants from config/tenants.yaml")
db_app = typer.Typer(no_args_is_help=True, help="Database migrations and checks")
app.add_typer(tenants_app, name="tenants")
app.add_typer(db_app, name="db")


def _settings() -> Settings:
    settings = Settings()
    steps.configure(settings)
    return settings


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
