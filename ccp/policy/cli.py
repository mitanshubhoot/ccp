"""``ccp policy gap|draft|drafts``: gap analysis and drafts for one control."""

from __future__ import annotations

import json

import typer

from ccp.config import Settings
from ccp.evaluators.tier2 import Providers
from ccp.ingest.consumers import ParkedError
from ccp.observability import steps
from ccp.policy import runner, schema
from ccp.storage import db
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

app = typer.Typer(no_args_is_help=True, help="The Policy Agent: gap analysis and drafts")


def _settings() -> Settings:
    settings = Settings()
    steps.configure(settings)
    return settings


def _tenant(settings: Settings, tenant_id: str | None) -> TenantContext:
    chosen = tenant_id or settings.ccp_tenant
    if not chosen:
        raise typer.BadParameter("pass --tenant or set CCP_TENANT")
    return TenantContext.for_id(settings, chosen)


def _providers(settings: Settings) -> Providers:
    providers = Providers.from_settings(settings)
    if providers.llm is None:
        typer.echo("the Policy Agent needs ANTHROPIC_API_KEY", err=True)
        raise typer.Exit(code=2)
    return providers


def _render(report: schema.GapReport) -> str:
    return json.dumps(
        {
            "report_id": report.report_id,
            "control_id": report.control_id,
            "summary": report.summary,
            "confidence": report.confidence,
            "gaps": [g.model_dump(mode="json") for g in report.gaps],
            "guardrail_events": list(report.guardrail_events),
        },
        indent=2,
        default=str,
    )


@app.command("gap")
def gap(
    control: str = typer.Option(..., "--control"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Which requirements the control maps to are addressed by policy text, and what is missing."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    providers = _providers(settings)
    pool = db.app_pool(settings)
    try:
        report = runner.gap(ScopedDB(pool, ctx), settings, control, providers)
    except ParkedError as exc:
        typer.echo(f"parked: {exc}", err=True)
        raise typer.Exit(code=3) from None
    finally:
        pool.close()
    typer.echo(_render(report))


@app.command("draft")
def draft(
    control: str = typer.Option(..., "--control"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """A Markdown draft closing the gaps, written under data/drafts/ and sent for approval."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    providers = _providers(settings)
    pool = db.app_pool(settings)
    try:
        report, result = runner.draft(ScopedDB(pool, ctx), settings, control, providers)
    except ParkedError as exc:
        typer.echo(f"parked: {exc}", err=True)
        raise typer.Exit(code=3) from None
    finally:
        pool.close()
    typer.echo(_render(report))
    typer.echo(f"draft {result.draft_id} written to {result.path}; approval {result.approval_id}")
    for event in result.scrub_events:
        typer.echo(f"scrubbed {event}", err=True)


@app.command("drafts")
def drafts(
    tenant: str | None = typer.Option(None, "--tenant"),
    control: str | None = typer.Option(None, "--control"),
) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        rows = schema.list_drafts(ScopedDB(pool, ctx), control)
    finally:
        pool.close()
    if not rows:
        typer.echo("no drafts")
    for row in rows:
        typer.echo(
            f"{row['draft_id']}  {row['control_id']:<24} {row['path']}"
            f"  approval={row['approval_id']}"
        )
