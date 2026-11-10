"""``ccp remediate ...``: propose, list, show, execute (dry run by default), rollback (behind its
own approval, dry run by default), audit. There is no `simulate` command: the fixture hook is
test-only and unreachable from here."""

from __future__ import annotations

import json

import typer

from ccp.config import Settings
from ccp.observability import steps
from ccp.remediation import proposals, runner
from ccp.storage import db
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

app = typer.Typer(no_args_is_help=True, help="Remediation proposals: dry-run by default")


def _settings() -> Settings:
    settings = Settings()
    steps.configure(settings)
    return settings


def _tenant(settings: Settings, tenant_id: str | None) -> TenantContext:
    chosen = tenant_id or settings.ccp_tenant
    if not chosen:
        raise typer.BadParameter("pass --tenant or set CCP_TENANT")
    return TenantContext.for_id(settings, chosen)


def _prompt(prompt: str) -> str:
    return str(typer.prompt(prompt, default="", show_default=False))


@app.command("propose")
def propose(
    verdict: str = typer.Option(..., "--verdict", help="verdict id to propose for"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Rule-fallback proposals for one verdict; each waits in the approval stage."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        summary = runner.propose_for_verdict_id(ScopedDB(pool, ctx), verdict)
    finally:
        pool.close()
    for p in summary.proposals:
        typer.echo(f"{p.proposal_id}  {p.action:<26} {json.dumps(p.target)}  [{p.status}]")
    for event in summary.rejected:
        typer.echo(f"rejected {event}", err=True)
    if not summary.proposals:
        typer.echo("no proposals")


@app.command("list")
def list_cmd(
    tenant: str | None = typer.Option(None, "--tenant"),
    status: str | None = typer.Option(None, "--status"),
) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        rows = proposals.list_all(ScopedDB(pool, ctx), status)
    finally:
        pool.close()
    if not rows:
        typer.echo("no proposals")
    for p in rows:
        typer.echo(f"{p.proposal_id}  {p.status:<12} {p.action:<26} {json.dumps(p.target)}")


@app.command("show")
def show(proposal_id: str, tenant: str | None = typer.Option(None, "--tenant")) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        p = proposals.get(ScopedDB(pool, ctx), proposal_id)
    finally:
        pool.close()
    if p is None:
        raise typer.BadParameter(f"no proposal {proposal_id!r}")
    typer.echo(json.dumps(runner.describe(p, ctx.settings.github_org), indent=2, default=str))


@app.command("execute")
def execute(
    proposal_id: str,
    live: bool = typer.Option(False, "--live", help="gate 2; dry run without it"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Dry run by default: records the exact call. Live needs all five gates."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        result = runner.execute(
            ScopedDB(pool, ctx), settings, proposal_id, live=live, confirm=_prompt
        )
    finally:
        pool.close()
    typer.echo(f"{proposal_id}: {result.mode}; {result.detail}")
    if result.proposal.planned_call:
        typer.echo(f"call: {json.dumps(result.proposal.planned_call)}")


@app.command("rollback")
def rollback(
    proposal_id: str,
    live: bool = typer.Option(False, "--live", help="gate 2; dry run without it"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Needs an approved rollback item first (the first call opens one). Live needs all five
    gates, with `restore <login> to <org>/<repo>` as the typed confirmation."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        result = runner.rollback(
            ScopedDB(pool, ctx), settings, proposal_id, live=live, confirm=_prompt
        )
    finally:
        pool.close()
    typer.echo(f"{proposal_id}: {result.mode}; {result.detail}")


@app.command("audit")
def audit(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Proposals that acted without a matching approval. Exits 1 on any."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        problems = runner.audit(ScopedDB(pool, ctx))
    finally:
        pool.close()
    for problem in problems:
        typer.echo(problem)
    if problems:
        raise typer.Exit(1)
    typer.echo("every remediation action has a matching approval")
