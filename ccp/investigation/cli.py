"""``ccp investigate ...``: run the Investigator over pending verdicts, or show one."""

from __future__ import annotations

import json

import typer

from ccp.config import Settings
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.tier2 import Providers
from ccp.investigation import runner, schema
from ccp.observability import steps
from ccp.storage import db
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

app = typer.Typer(
    no_args_is_help=True, help="The Investigator: root cause, missing evidence, proposals"
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


@app.command("run")
def run(
    tenant: str | None = typer.Option(None, "--tenant"),
    verdict: str | None = typer.Option(
        None, "--verdict", help="one verdict id instead of the topic"
    ),
) -> None:
    """Investigate every pending fail/needs_review verdict (or one verdict id)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    providers = Providers.from_settings(settings)
    if providers.llm is None:
        typer.echo("the Investigator needs ANTHROPIC_API_KEY; nothing was investigated", err=True)
        raise typer.Exit(code=2)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        if verdict:
            v = verdicts.load(scoped, verdict)
            if v is None:
                raise typer.BadParameter(f"no verdict {verdict!r}")
            investigation = runner.investigate(scoped, settings, v, providers)
            typer.echo(_render(investigation))
            return
        summary = runner.drain(scoped, settings, providers)
    finally:
        pool.close()
    typer.echo(
        f"consumed {summary.consumed}, queued {summary.enqueued},"
        f" investigated {summary.investigated}, parked {summary.parked},"
        f" skipped {summary.skipped}, proposals {summary.proposals},"
        f" evidence requests {summary.evidence_requests}"
    )
    for event in summary.rejected_targets:
        typer.echo(f"rejected {event}", err=True)


@app.command("show")
def show(
    verdict: str = typer.Option(..., "--verdict"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        investigation = schema.for_verdict(ScopedDB(pool, ctx), verdict)
    finally:
        pool.close()
    if investigation is None:
        typer.echo(f"no investigation for verdict {verdict}")
        raise typer.Exit(code=1)
    typer.echo(_render(investigation))


@app.command("list")
def list_cmd(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        rows = schema.list_all(ScopedDB(pool, ctx))
    finally:
        pool.close()
    if not rows:
        typer.echo("no investigations")
    for inv in rows:
        typer.echo(
            f"{inv.investigation_id}  {inv.root_cause_category:<20}"
            f" verdict={inv.verdict_id} actions={len(inv.proposed_actions)}"
        )


def _render(inv: schema.Investigation) -> str:
    return json.dumps(
        {
            "investigation_id": inv.investigation_id,
            "verdict_id": inv.verdict_id,
            "root_cause_category": inv.root_cause_category,
            "root_cause": inv.root_cause,
            "missing_evidence": [m.model_dump(mode="json") for m in inv.missing_evidence],
            "proposed_actions": [p.model_dump(mode="json") for p in inv.proposed_actions],
            "confidence": inv.confidence,
            "guardrail_events": list(inv.guardrail_events),
        },
        indent=2,
        default=str,
    )
