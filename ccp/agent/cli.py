"""``ccp agent ...``: run, inspect, and resume graph threads from a separate process
(spec.md Layer 8 "Checkpointing and the interrupt"). ``resume`` goes through the approval
stage like ``ccp approvals approve``: the decision is recorded on the thread's open approval
item first, and the graph resumes from that record (A-95)."""

from __future__ import annotations

import json

import typer

from ccp import auth
from ccp.agent import decisions, threads, tier3
from ccp.approvals import store as approvals
from ccp.config import Settings
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
    token: str = typer.Option(..., "--token", envvar="CCP_TOKEN", help="your sign-in token"),
    status: str | None = typer.Option(
        None, "--status", help="with override: pass|fail|partial|needs_review"
    ),
    note: str | None = typer.Option(None, "--note"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Record a signed-in member's decision on the thread's open approval item, then resume
    from it; persists the resulting verdict (A-130)."""
    settings = _settings()
    try:
        who = auth.authenticate(settings, token)
    except auth.InvalidToken as exc:
        typer.echo(f"refused: sign-in token refused: {exc}", err=True)
        raise typer.Exit(2) from None
    try:
        chosen = decisions.build(decision, who.label, note, status)
    except decisions.InvalidDecision as exc:
        raise typer.BadParameter(str(exc)) from None
    ctx = _guarded_tenant(settings, thread, tenant)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        waiting = [
            i
            for i in approvals.list_items(scoped, decisions.KIND, open_only=True)
            if i.ref_id == thread
        ]
        if len(waiting) != 1:
            typer.echo(
                f"refused: thread {thread!r} has {len(waiting)} open approval items; decide one"
                " with `ccp approvals approve <id>`",
                err=True,
            )
            raise typer.Exit(2)
        item = waiting[0]
        try:
            result = decisions.decide(
                scoped, settings, item.approval_id, event="approved", decision=chosen, principal=who
            )
        except (LookupError, ValueError, PermissionError, decisions.PayloadChanged) as exc:
            typer.echo(f"refused: {exc}", err=True)
            raise typer.Exit(2) from None
    finally:
        pool.close()
    if result.verdict is None or result.error is not None:
        typer.echo(
            f"{thread}: decision recorded on {item.approval_id}; resume failed: {result.error}."
            f" Run `ccp approvals resume {item.approval_id}` to apply it"
        )
        raise typer.Exit(1)
    verdict = result.verdict
    typer.echo(
        f"{thread}: {verdict.status.value} (verdict {verdict.verdict_id},"
        f" inserted={result.inserted}, approval {item.approval_id})"
    )
    typer.echo(verdict.reasoning)


@app.command("replay")
def replay(
    thread: str = typer.Option(..., "--thread"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Run a recorded tier 3 run again from its log alone (A-109): every request must come
    out as recorded. Exits 1 on the first request the log does not hold."""
    settings = _settings()
    ctx = _guarded_tenant(settings, thread, tenant)
    pool = db.app_pool(settings)
    try:
        report = tier3.replay(ScopedDB(pool, ctx), settings, thread)
    except LookupError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(2) from None
    finally:
        pool.close()
    typer.echo(f"replayed {thread}: {report.served} recorded steps, trajectory {report.trajectory}")
    if report.diverged is not None:
        typer.echo(f"diverged: {report.diverged}", err=True)
        raise typer.Exit(1)
    typer.echo("every request matched the record")


@app.command("lint")
def lint() -> None:
    """The Rule-of-Two lint over the tool policy (A-114): exits 1 when a principal could read
    untrusted content, hold private data and change state."""
    from ccp.agent import authz
    from ccp.agent.tools import TOOL_SPECS

    problems = authz.rule_of_two(TOOL_SPECS)
    for problem in problems:
        typer.echo(problem, err=True)
    if problems:
        raise typer.Exit(1)
    typer.echo("rule of two: no principal reads untrusted content, holds private data and acts")
