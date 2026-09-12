"""The CLI: ``python -m ccp ...`` or ``ccp ...`` after ``uv sync``."""

from __future__ import annotations

from datetime import datetime

import typer

from ccp.config import Settings
from ccp.connectors.base import parse_ts
from ccp.connectors.registry import CONNECTOR_NAMES, SOURCE_KINDS, build_connector
from ccp.evals import runner as eval_runner
from ccp.identity import resolver
from ccp.ingest import worker
from ccp.ingest.landing import ingest_tenant
from ccp.normalize import consumer as normalize_consumer
from ccp.normalize import repository
from ccp.observability import steps
from ccp.storage import db
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="ccp: compliance companion platform"
)
tenants_app = typer.Typer(no_args_is_help=True, help="Tenants from config/tenants.yaml")
db_app = typer.Typer(no_args_is_help=True, help="Database migrations and checks")
connectors_app = typer.Typer(no_args_is_help=True, help="Pull provider data as SourceEvents")
ingest_app = typer.Typer(no_args_is_help=True, help="Land events in the raw zone and event log")
worker_app = typer.Typer(no_args_is_help=True, help="Run consumers over the event log")
evals_app = typer.Typer(no_args_is_help=True, help="Golden-dataset evaluation harness")
normalize_app = typer.Typer(no_args_is_help=True, help="Map raw events into canonical entities")
identity_app = typer.Typer(no_args_is_help=True, help="Identity resolution and the review queue")
app.add_typer(tenants_app, name="tenants")
app.add_typer(db_app, name="db")
app.add_typer(connectors_app, name="connectors")
app.add_typer(ingest_app, name="ingest")
app.add_typer(worker_app, name="worker")
app.add_typer(evals_app, name="evals")
app.add_typer(normalize_app, name="normalize")
app.add_typer(identity_app, name="identity")


@identity_app.command("resolve")
def identity_resolve(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Block, gate, score, and route every candidate pair; log every decision."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        summary = resolver.resolve_tenant(ScopedDB(pool, ctx))
    finally:
        pool.close()
    typer.echo(f"compared {summary.compared}, unchanged {summary.skipped_unchanged}")
    for route_name, n in sorted(summary.routes.items()):
        typer.echo(f"{route_name:<14} {n}")


@identity_app.command("review")
def identity_review(
    tenant: str | None = typer.Option(None, "--tenant"),
    list_open: bool = typer.Option(False, "--list", help="List open review items"),
    resolve: str | None = typer.Option(None, "--resolve", help="Decision id to close"),
    outcome: str | None = typer.Option(None, "--as", help="merge | distinct"),
    reviewer: str | None = typer.Option(None, "--reviewer"),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """List open review items, or close one as merge or distinct."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        if resolve:
            if not outcome or not reviewer:
                raise typer.BadParameter("--resolve needs --as merge|distinct and --reviewer")
            resolver.resolve_review(scoped, resolve, outcome, reviewer, note)
            typer.echo(f"{resolve}: {outcome} by {reviewer}")
            return
        items = resolver.open_reviews(scoped)
        if not items:
            typer.echo("no open review items")
        for item in items:
            typer.echo(
                f"{item.decision_id}  score={item.score}  "
                f"{item.left_entity_id}  <->  {item.right_entity_id}"
            )
    finally:
        pool.close()


@normalize_app.command("run")
def normalize_run(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Drain every raw topic for a tenant into the entity tables."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        processed = normalize_consumer.normalize_all(scoped, settings)
        counts = {
            "persons": len(repository.current_persons(scoped)),
            "access_grants": len(repository.current_grants(scoped)),
            "assets": len(repository.current_assets(scoped)),
            "evidence": len(repository.current_evidence(scoped)),
            "vendors": len(repository.current_vendors(scoped)),
        }
    finally:
        pool.close()
    typer.echo(f"processed {processed} raw records")
    for name, n in counts.items():
        typer.echo(f"{name:<14} {n}")


@ingest_app.command("run")
def ingest_run(
    tenant: str | None = typer.Option(None, "--tenant"),
    connector: list[str] = typer.Option([], "--connector", help="repeatable; default all"),
    source: str = typer.Option("fixture", "--source", help="fixture | live"),
) -> None:
    """Pull every connector for a tenant, land in the raw zone, append to the event log."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    names = [n for n in CONNECTOR_NAMES if not connector or n in connector]
    kind = next((k for k in SOURCE_KINDS if k == source), None)
    if kind is None:
        raise typer.BadParameter("source must be fixture or live")
    pool = db.app_pool(settings)
    try:
        summary = ingest_tenant(settings, ScopedDB(pool, ctx), ctx, names, kind)
    finally:
        pool.close()
    for name, n in summary.by_connector.items():
        typer.echo(f"{name:<8} landed {n}")
    typer.echo(f"total landed {summary.landed}, duplicates {summary.duplicates}")


@worker_app.command("run")
def worker_run(
    consumer: str = typer.Option(..., "--consumer"),
    tenant: str | None = typer.Option(None, "--tenant", help="default: every tenant"),
    once: bool = typer.Option(False, "--once", help="drain and exit instead of polling"),
    poll_seconds: float = typer.Option(2.0, "--poll-seconds"),
) -> None:
    """Run a registered consumer for one tenant or all tenants."""
    settings = _settings()
    tenants = [_tenant(settings, tenant)] if tenant else TenantContext.all(settings)
    processed = worker.run(settings, consumer, tenants, once=once, poll_seconds=poll_seconds)
    typer.echo(f"{consumer}: processed {processed} records")


@worker_app.command("list")
def worker_list() -> None:
    """List registered consumers."""
    for name in sorted(worker.CONSUMERS):
        typer.echo(name)


@evals_app.command("run")
def evals_run(
    level: str | None = typer.Option(None, "--level", help="component | workflow | e2e"),
    tag: str | None = typer.Option(None, "--tag"),
    case_id: str | None = typer.Option(None, "--id"),
) -> None:
    """Run the golden dataset and write evals/runs/latest.json. Exit code 1 on any failure."""
    settings = _settings()
    cases, hashes = eval_runner.load_cases(settings.ccp_golden_dir)
    chosen = eval_runner.select(cases, level=level, tag=tag, case_id=case_id)
    if not chosen:
        raise typer.BadParameter("no golden cases matched")
    report = eval_runner.run(settings, chosen, hashes)
    path = eval_runner.write_report(report, settings.ccp_eval_runs_dir)
    typer.echo(eval_runner.format_table(report))
    typer.echo(f"report: {path}")
    if not report.all_passed:
        raise typer.Exit(code=1)


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
