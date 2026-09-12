"""``ccp agent ...``: run, inspect, and resume graph threads from a separate process
(spec.md Layer 8 "Checkpointing and the interrupt")."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import typer

from ccp.agent import threads, tier3
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.observability import steps
from ccp.storage import checkpoints, db
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

app = typer.Typer(no_args_is_help=True, help="Graph threads: pending, inspect, resume")


def _settings() -> Settings:
    settings = Settings()
    steps.configure(settings)
    return settings


def _tenant(settings: Settings, tenant_id: str | None) -> TenantContext:
    chosen = tenant_id or settings.ccp_tenant
    if not chosen:
        raise typer.BadParameter("pass --tenant or set CCP_TENANT")
    return TenantContext.for_id(settings, chosen)


def _guarded_tenant(settings: Settings, thread: str, tenant: str | None) -> TenantContext:
    """The thread id names its tenant; an explicit --tenant must agree, before any saver access."""
    owner = checkpoints.tenant_of_thread(thread)
    if tenant is not None and tenant != owner:
        raise typer.BadParameter(f"thread {thread!r} belongs to tenant {owner!r}, not {tenant!r}")
    try:
        return TenantContext.for_id(settings, owner)
    except KeyError:
        raise typer.BadParameter(f"thread {thread!r} names an unknown tenant {owner!r}") from None


@app.command("pending")
def pending(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Threads stopped at the human gate."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        rows = threads.pending(ScopedDB(pool, ctx))
    finally:
        pool.close()
    if not rows:
        typer.echo("no pending threads")
    for row in rows:
        draft = (row.interrupt or {}).get("draft", {})
        typer.echo(
            f"{row.thread_id}  {draft.get('status')}  confidence={draft.get('confidence')}"
            f"  since {row.updated_at.isoformat()}"
        )


@app.command("threads")
def list_threads(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Recent threads of every status."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        rows = threads.all_threads(ScopedDB(pool, ctx))
    finally:
        pool.close()
    for row in rows:
        typer.echo(f"{row.status:<12} {row.thread_id}  {' -> '.join(row.trajectory)}")


@app.command("inspect")
def inspect(
    thread: str = typer.Option(..., "--thread"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """The interrupt payload and trajectory of one thread."""
    settings = _settings()
    ctx = _guarded_tenant(settings, thread, tenant)
    pool = db.app_pool(settings)
    try:
        row = threads.get(ScopedDB(pool, ctx), thread)
    finally:
        pool.close()
    if row is None:
        raise typer.BadParameter(f"no thread {thread!r}")
    typer.echo(
        json.dumps(
            {
                "thread_id": row.thread_id,
                "status": row.status,
                "test_id": row.test_id,
                "subject_key": row.subject_key,
                "trajectory": row.trajectory,
                "interrupt": row.interrupt,
                "decision": row.decision,
                "verdict_id": row.verdict_id,
                "error": row.error,
            },
            indent=2,
            default=str,
        )
    )


@app.command("resume")
def resume(
    thread: str = typer.Option(..., "--thread"),
    decision: str = typer.Option(..., "--decision", help="approve | override | regather"),
    reviewer: str = typer.Option(..., "--reviewer"),
    status: str | None = typer.Option(
        None, "--status", help="with override: pass|fail|partial|needs_review"
    ),
    note: str | None = typer.Option(None, "--note"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Continue an interrupted thread with a human decision; persists the resulting verdict."""
    if decision not in ("approve", "override", "regather"):
        raise typer.BadParameter("decision must be approve, override, or regather")
    if decision == "override" and not status:
        raise typer.BadParameter("--status is required with override")
    if not reviewer.strip():
        raise typer.BadParameter("a reviewer name is required")
    settings = _settings()
    ctx = _guarded_tenant(settings, thread, tenant)
    payload = {
        "action": decision,
        "reviewer": reviewer,
        "note": note,
        "status": status,
        "decided_at": datetime.now(UTC).isoformat(),
    }
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        verdict = tier3.resume(scoped, settings, thread, payload)
        inserted = evaluations.commit_verdict(scoped, verdict)
    finally:
        pool.close()
    typer.echo(
        f"{thread}: {verdict.status.value} (verdict {verdict.verdict_id}, inserted={inserted})"
    )
    typer.echo(verdict.reasoning)
