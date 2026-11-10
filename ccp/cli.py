"""The CLI: ``python -m ccp ...`` or ``ccp ...`` after ``uv sync``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer

from ccp.agent import cli as agent_cli
from ccp.agent import tier3 as _tier3  # noqa: F401 - registers tier 3
from ccp.approvals import cli as approvals_cli
from ccp.compliance_state import reader as state_reader
from ccp.compliance_state import run as pipeline
from ccp.compliance_state import writer as state_writer  # noqa: F401 - registers the state consumer
from ccp.config import Settings
from ccp.connectors.base import parse_ts
from ccp.connectors.registry import CONNECTOR_NAMES, SOURCE_KINDS, build_connector
from ccp.copilot import cli as copilot_cli
from ccp.evals import calibration as judge_calibration
from ccp.evals import capture, regress
from ccp.evals import labels as eval_labels
from ccp.evals import runner as eval_runner
from ccp.evals.schema import GoldenCase
from ccp.evaluators import clock as clock_producer
from ccp.evaluators import impact as impact_step
from ccp.evaluators import runner as evaluations
from ccp.evaluators import thresholds, tier2
from ccp.evaluators import verdict as verdicts
from ccp.identity import resolver
from ccp.ingest import ops as queue_ops
from ccp.ingest import worker
from ccp.ingest.landing import ingest_tenant
from ccp.investigation import cli as investigate_cli
from ccp.knowledge import graph as knowledge_graph
from ccp.knowledge import rollup as knowledge_rollup
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import try_build_client
from ccp.normalize import consumer as normalize_consumer
from ccp.normalize import repository
from ccp.observability import steps
from ccp.policy import cli as policy_cli
from ccp.remediation import cli as remediate_cli
from ccp.replay import rebuild as replay
from ccp.retrieval import calibration, distractors
from ccp.retrieval import config as retrieval_cfg
from ccp.retrieval import index as retrieval_index
from ccp.retrieval import metrics as retrieval_metrics
from ccp.retrieval.hybrid import retrieve
from ccp.storage import bitemporal, db
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
knowledge_app = typer.Typer(no_args_is_help=True, help="Frameworks, controls, tests")
evaluate_app = typer.Typer(no_args_is_help=True, help="Trigger and run evaluations")
index_app = typer.Typer(no_args_is_help=True, help="The retrieval index over current evidence")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(tenants_app, name="tenants")
app.add_typer(evaluate_app, name="evaluate")
app.add_typer(index_app, name="index")
app.add_typer(agent_cli.app, name="agent")
app.add_typer(approvals_cli.app, name="approvals")
app.add_typer(investigate_cli.app, name="investigate")
app.add_typer(policy_cli.app, name="policy")
app.command("copilot")(copilot_cli.copilot)
app.add_typer(remediate_cli.app, name="remediate")
state_app = typer.Typer(no_args_is_help=True, help="The bitemporal compliance state")
app.add_typer(state_app, name="state")


@app.command("run")
def run_pipeline(
    tenant: str | None = typer.Option(None, "--tenant"),
    source: str = typer.Option("fixture", "--source", help="fixture | live"),
    no_ingest: bool = typer.Option(False, "--no-ingest", help="skip pulling connectors"),
) -> None:
    """Ingest, normalize, resolve, index, evaluate, and write compliance state for one tenant."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    kind = next((k for k in SOURCE_KINDS if k == source), None)
    if kind is None:
        raise typer.BadParameter("source must be fixture or live")
    pool = db.app_pool(settings)
    try:
        report_ = pipeline.run(ScopedDB(pool, ctx), settings, source=kind, ingest=not no_ingest)
    finally:
        pool.close()
    typer.echo(pipeline.format_report(report_))


@state_app.command("current")
def state_current(
    tenant: str | None = typer.Option(None, "--tenant"),
    subject: str | None = typer.Option(None, "--subject", help="a subject key, e.g. HR-1|person:x"),
) -> None:
    """The open row per subject (or one subject)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        rows = (
            [r for r in [state_reader.current_state(scoped, subject)] if r is not None]
            if subject
            else state_reader.heads(scoped)
        )
    finally:
        pool.close()
    if not rows:
        typer.echo("NO EVALUATIONS YET" if not subject else f"no state for {subject}")
    for row in rows:
        typer.echo(state_reader.format_row(row))


@state_app.command("history")
def state_history(
    subject: str = typer.Option(..., "--subject"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Every row ever recorded for a subject, in recorded order."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        rows = state_reader.chain(ScopedDB(pool, ctx), subject)
    finally:
        pool.close()
    if not rows:
        typer.echo(f"no state for {subject}")
    for row in rows:
        typer.echo(state_reader.format_row(row))


@state_app.command("asof")
def state_asof(
    subject: str = typer.Option(..., "--subject"),
    valid_at: str = typer.Option(..., "--valid-at", help="ISO timestamp: the moment asked about"),
    known_at: str | None = typer.Option(
        None, "--known-at", help="ISO timestamp: what was known then"
    ),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Answer both audit questions: were we compliant then, and did we know it then."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    valid = parse_ts(valid_at)
    known = parse_ts(known_at) if known_at else datetime.now(UTC)
    pool = db.app_pool(settings)
    try:
        row = state_reader.state_as_of(ScopedDB(pool, ctx), subject, valid, known)
    finally:
        pool.close()
    typer.echo(
        state_reader.format_row(row)
        if row
        else f"no state for {subject} at {valid_at} as known at {known.isoformat()}"
    )


@evaluate_app.command("run")
def evaluate_run(
    tenant: str | None = typer.Option(None, "--tenant"),
    test: list[str] = typer.Option(
        [], "--test", help="repeatable; default every tier 1 and 2 test"
    ),
    impact: bool = typer.Option(
        False, "--impact", help="drain the entities topic through the impact step instead"
    ),
    no_index: bool = typer.Option(False, "--no-index", help="skip refreshing the retrieval index"),
) -> None:
    """Refresh the index, enqueue every (test, subject) pair once (or run the impact step),
    then drain them. Tier 2 runs when ANTHROPIC_API_KEY is set and parks otherwise."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    providers = tier2.Providers.from_settings(settings)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        if not no_index:
            refreshed = retrieval_index.refresh(scoped, settings, providers.embedder, providers.llm)
            typer.echo(
                f"index: {refreshed.chunks} chunks ({refreshed.upserted} upserted,"
                f" {refreshed.unchanged} unchanged) embed_model={refreshed.embed_model}"
            )
        if impact:  # the change path: link new accounts to people before the impact step (A-101)
            resolver.resolve_tenant(scoped)
            consumer = impact_step.ImpactConsumer(scoped)
            while consumer.run_once():
                pass
            enqueued = consumer.enqueued
        else:
            enqueued = evaluations.enqueue_all(scoped, settings, tests=tuple(test) or None)
        summary = evaluations.drain(scoped, settings, providers=providers)
        parked = evaluations.parked_count(scoped)
    finally:
        pool.close()
    if providers.llm is None:
        typer.echo("AI tiers parked: ANTHROPIC_API_KEY is not set", err=True)
    typer.echo(f"enqueued {enqueued}, evaluated {summary.evaluated}, parked {parked}")
    for status, n in sorted(summary.by_status.items()):
        typer.echo(f"{status:<13} {n}")


@index_app.command("rebuild")
def index_rebuild(
    tenant: str | None = typer.Option(None, "--tenant"),
    contextual: bool | None = typer.Option(
        None, "--contextual/--no-contextual", help="default: RETRIEVAL_CONTEXTUAL"
    ),
) -> None:
    """Drop and re-chunk, re-enrich (flag), and re-embed every current evidence row."""
    _index(tenant, rebuild=True, contextual=contextual)


@index_app.command("refresh")
def index_refresh(
    tenant: str | None = typer.Option(None, "--tenant"),
    contextual: bool | None = typer.Option(None, "--contextual/--no-contextual"),
) -> None:
    """Index new or changed evidence only; unchanged chunks are a no-op."""
    _index(tenant, rebuild=False, contextual=contextual)


def _index(tenant: str | None, *, rebuild: bool, contextual: bool | None) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    providers = tier2.Providers.from_settings(settings)
    pool = db.app_pool(settings)
    try:
        summary = retrieval_index.refresh(
            ScopedDB(pool, ctx),
            settings,
            providers.embedder,
            providers.llm,
            contextual_enrichment=contextual,
            rebuild=rebuild,
        )
    finally:
        pool.close()
    typer.echo(
        f"embed_model={summary.embed_model} chunker={summary.chunker_version} "
        f"evidence={summary.evidence_rows} chunks={summary.chunks} upserted={summary.upserted} "
        f"unchanged={summary.unchanged} deleted={summary.deleted} embedded={summary.embedded} "
        f"cache_hits={summary.embedding_cache_hits} enriched={summary.enriched} "
        f"usd={summary.usd:.4f}"
    )
    for event in summary.events:
        typer.echo(f"event {event}", err=True)


@index_app.command("stats")
def index_stats(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Chunk counts by evidence type, embedding models present, distractors."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        stats = retrieval_index.stats(ScopedDB(pool, ctx))
    finally:
        pool.close()
    for key, value in stats.items():
        typer.echo(f"{key}: {value}")


@index_app.command("distractors")
def index_distractors(
    tenant: str | None = typer.Option(None, "--tenant"),
    count: int = typer.Option(300, "--count"),
    clear: bool = typer.Option(False, "--clear", help="remove them instead"),
) -> None:
    """Install (or clear) generated distractor chunks for retrieval evaluation and calibration."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    providers = tier2.Providers.from_settings(settings)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        if clear:
            typer.echo(f"cleared {distractors.clear(scoped)} distractor chunks")
            return
        total = distractors.install(scoped, providers.embedder, count)
    finally:
        pool.close()
    typer.echo(f"{total} distractor chunks present")


@index_app.command("calibrate")
def index_calibrate(
    tenant: str | None = typer.Option(None, "--tenant"),
    write: bool = typer.Option(True, "--write/--no-write", help="update config/retrieval.yaml"),
) -> None:
    """Measure the distractor noise floor for the configured providers; print the metrics."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    providers = tier2.Providers.from_settings(settings)
    queries = retrieval_metrics.load_queries()
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        if retrieval_index.stats(scoped)["distractors"] == 0:
            distractors.install(scoped, providers.embedder, 300)
        report = retrieval_metrics.evaluate_queries(
            scoped,
            providers.embedder,
            queries,
            reranker=providers.reranker,
            config=providers.retrieval,
        )
        cal = calibration.calibrate(
            scoped,
            providers.embedder,
            queries,
            reranker=providers.reranker,
            config=providers.retrieval,
        )
    finally:
        pool.close()
    for key, value in report.summary().items():
        typer.echo(f"{key}: {value}")
    typer.echo(f"noise floor {cal.key} over {cal.samples} queries: {cal.thresholds.percentiles}")
    if write:
        retrieval_cfg.save(calibration.record(providers.retrieval, cal))
        typer.echo(f"wrote {retrieval_cfg.CONFIG_PATH}")


@index_app.command("search")
def index_search(
    query: str = typer.Argument(..., help="the retrieval query"),
    evidence_type: str = typer.Option(..., "--type"),
    tenant: str | None = typer.Option(None, "--tenant"),
    subject: str | None = typer.Option(None, "--subject"),
) -> None:
    """Run the hybrid query and print the candidates with both ranks and the fused score."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    providers = tier2.Providers.from_settings(settings)
    pool = db.app_pool(settings)
    try:
        result = retrieve(
            ScopedDB(pool, ctx),
            providers.embedder,
            query,
            evidence_type=evidence_type,
            subject_entity_id=subject,
            reranker=providers.reranker,
            config=providers.retrieval,
        )
    finally:
        pool.close()
    for event in result.events:
        typer.echo(f"event {event}", err=True)
    for c in result.chunks:
        typer.echo(
            f"{c.chunk_id} fts={c.fts_rank} cos={c.cos_rank} fused={c.fused_score:.4f}"
            f" rerank={c.rerank_score} page={c.page} {c.evidence_entity_id}"
        )
        typer.echo("    " + c.text[:160].replace("\n", " "))


@evaluate_app.command("list")
def evaluate_list(
    tenant: str | None = typer.Option(None, "--tenant"),
    test: str | None = typer.Option(None, "--test"),
) -> None:
    """Print the newest verdict per subject."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        rows = verdicts.latest_per_subject(ScopedDB(pool, ctx), test)
    finally:
        pool.close()
    if not rows:
        typer.echo("no verdicts")
    for v in rows:
        typer.echo(f"{v.status.value:<13} {v.subject_key:<52} {v.reasoning}")


@evaluate_app.command("clock")
def evaluate_clock(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """One clock tick: enqueue subjects whose verdict or evidence has gone stale."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        summary = clock_producer.tick(ScopedDB(pool, ctx))
    finally:
        pool.close()
    typer.echo(f"enqueued {summary.enqueued}")
    for reason, n in sorted(summary.reasons.items()):
        typer.echo(f"{reason:<20} {n}")


@knowledge_app.command("show")
def knowledge_show() -> None:
    """Print the graph: each test, its tier, its controls, and the requirements they satisfy."""
    graph = knowledge_graph.load()
    for spec in graph.tests.values():
        controls = graph.controls_for_test(spec.test_id)
        reqs = sorted(r for c in controls for r in c.requirements)
        typer.echo(f"{spec.test_id:<5} tier {spec.tier}  {spec.title}")
        typer.echo(
            f"      controls={','.join(c.control_id for c in controls)}"
            f"  requirements={','.join(reqs)}"
        )


@knowledge_app.command("load")
def knowledge_load(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Upsert the graph into the reference tables (any tenant context opens the connection)."""
    settings = _settings()
    ctx = _tenant(settings, tenant or "acme")
    pool = db.app_pool(settings)
    try:
        counts = knowledge_graph.load_into_db(ScopedDB(pool, ctx), knowledge_graph.load())
    finally:
        pool.close()
    for name, n in counts.items():
        typer.echo(f"{name:<13} {n}")


@app.command("report")
def report(
    framework: str = typer.Option(..., "--framework", help="soc2 | iso27001"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Roll current compliance state up to a framework's requirements."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    graph = knowledge_graph.load()
    if framework not in graph.frameworks:
        raise typer.BadParameter(f"framework must be one of {', '.join(graph.frameworks)}")
    pool = db.app_pool(settings)
    try:
        heads = bitemporal.heads(ScopedDB(pool, ctx))
    finally:
        pool.close()
    typer.echo(knowledge_rollup.format_rollup(knowledge_rollup.rollup(graph, heads, framework)))


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


@worker_app.command("dead-letters")
def worker_dead_letters(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Dead records: parallel topics, and ordered topics per consumer (A-97)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        rows = queue_ops.dead_letters(ScopedDB(pool, ctx))
    finally:
        pool.close()
    if not rows:
        typer.echo("no dead letters")
    for row in rows:
        who = f" consumer={row.consumer}" if row.consumer else ""
        typer.echo(
            f"{row.offset}  {row.topic}{who}  tries={row.attempts}  {(row.error or '')[:160]}"
        )


@worker_app.command("replay")
def worker_replay(
    offset: int = typer.Option(..., "--offset"),
    by: str = typer.Option(..., "--by", help="who replays it; recorded"),
    consumer: str | None = typer.Option(None, "--consumer", help="ordered topics: only this one"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Replay a dead record: a parallel one goes back to pending, an ordered one is re-run by
    each consumer that dead-lettered it. Exit 1 when a re-run fails again."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        outcomes = queue_ops.replay(ScopedDB(pool, ctx), settings, offset, by, consumer)
    except (LookupError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        pool.close()
    for who, outcome in outcomes.items():
        typer.echo(f"{offset} {who}: {outcome}")
    if "failed" in outcomes.values():
        raise typer.Exit(1)


@worker_app.command("unpark")
def worker_unpark(
    topic: str = typer.Option("evaluations", "--topic"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Parked records back to pending (they park again if the budget is still spent)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        moved = queue_ops.unpark(ScopedDB(pool, ctx), topic)
    finally:
        pool.close()
    typer.echo(f"{topic}: {moved} record(s) unparked")


def _eval_selection(
    settings: Settings, suite: str, level: str | None, tag: str | None, case_id: str | None
) -> tuple[list[GoldenCase], dict[str, str]]:
    try:
        cases, hashes = eval_runner.all_cases(settings, suite=suite)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    chosen = eval_runner.select(cases, level=level, tag=tag, case_id=case_id)
    if not chosen:
        raise typer.BadParameter("no golden cases matched")
    return chosen, hashes


@evals_app.command("run")
def evals_run(
    level: str | None = typer.Option(None, "--level", help="component | workflow | e2e"),
    tag: str | None = typer.Option(None, "--tag"),
    case_id: str | None = typer.Option(None, "--id"),
    suite: str = typer.Option("golden", "--suite", help="golden | adversarial | all"),
    unit: bool = typer.Option(
        True, "--unit/--no-unit", help="run pytest tests/unit for the unit row"
    ),
    runs: int | None = typer.Option(
        None, "--runs", min=1, help="runs per model-backed case; pass^k needs all k (A-103)"
    ),
) -> None:
    """Run the golden dataset and write evals/runs/latest.json. Exit code 1 on any failure."""
    settings = _settings()
    chosen, hashes = _eval_selection(settings, suite, level, tag, case_id)
    report = eval_runner.run(settings, chosen, hashes, unit=unit, runs=runs)
    path = eval_runner.write_report(report, settings.ccp_eval_runs_dir)
    typer.echo(eval_runner.format_table(report))
    typer.echo(f"report: {path}")
    if not report.all_passed:
        raise typer.Exit(code=1)


@evals_app.command("calibrate")
def evals_calibrate(
    bootstrap: int = typer.Option(1000, "--bootstrap", help="bootstrap resamples per interval"),
    bias_on: int | None = typer.Option(None, "--bias-on", help="bias checks on the first N cases"),
) -> None:
    """Run the judge over evals/labeled and report agreement and kappa with intervals."""
    settings = _settings()
    llm = try_build_client(settings)
    if llm is None:
        typer.echo("calibration needs ANTHROPIC_API_KEY (the judge model)", err=True)
        raise typer.Exit(code=2)
    cases = judge_calibration.load_labeled(settings.ccp_labeled_dir)
    report = judge_calibration.calibrate(
        llm,
        cases,
        model=settings.ccp_judge_model,
        tracker=BudgetTracker(RunBudget.from_settings(settings)),
        rubrics_dir=settings.ccp_rubrics_dir,
        n_boot=bootstrap,
        bias_on=bias_on,
    )
    typer.echo(judge_calibration.format_report(report))
    written = judge_calibration.write_report(report, settings.ccp_eval_runs_dir)
    typer.echo(f"report: {written}")


@evals_app.command("regress")
def evals_regress(
    baseline: Path | None = typer.Option(None, "--baseline", help="a run report; latest.json"),
    suite: str = typer.Option("all", "--suite", help="golden | adversarial | all"),
    unit: bool = typer.Option(False, "--unit/--no-unit"),
    strict: bool = typer.Option(
        False, "--strict", help="first failures and new failures block too (nightly)"
    ),
    runs: int | None = typer.Option(
        None, "--runs", min=1, help="runs per model-backed case (the nightly job uses 5)"
    ),
) -> None:
    """Rerun the suite and diff against the last run; non-zero on a deterministic case's
    pass-to-fail flip, an attack-success-rate increase, a p95 breach, or an AI test whose
    confidence bound crosses its gate (config/eval_gates.yaml), and with --strict also on a
    deterministic case that first runs and fails or a new one that fails. A model-backed
    case's flip is reported, not blocking: bounds gate those (A-103)."""
    settings = _settings()
    base_path = baseline or settings.ccp_eval_runs_dir / "latest.json"
    if not base_path.exists():
        typer.echo(f"no baseline at {base_path}; run `ccp evals run` first", err=True)
        raise typer.Exit(code=2)
    base = regress.load_report(base_path)
    chosen, hashes = _eval_selection(settings, suite, None, None, None)
    report = eval_runner.run(settings, chosen, hashes, unit=unit, runs=runs)
    gates = regress.load_gates(settings.ccp_eval_gates_path)
    reg = regress.diff(base, report, strict=strict, gates=gates)
    # a regressed run never becomes the baseline: the next run must still see the flip
    path = eval_runner.write_report(report, settings.ccp_eval_runs_dir, promote=reg.exit_code == 0)
    typer.echo(regress.format_regression(reg))
    typer.echo(f"report: {path}" + ("" if reg.exit_code == 0 else " (not promoted to latest)"))
    if reg.exit_code:
        raise typer.Exit(code=reg.exit_code)


@evals_app.command("labels")
def evals_labels(
    out: Path = typer.Option(..., "--out", help="a YAML file outside the repository"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Export the tenant's reviewer labels (eval or training consent) on tier 3 drafts as
    labelled-set intake: overall label from the decision, criteria for a person to add."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        written, skipped = eval_labels.export(ScopedDB(pool, ctx), out)
    except eval_labels.InsideRepository as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        pool.close()
    typer.echo(f"{written} labelled case(s) -> {out}")
    for why, count in sorted(skipped.items()):
        typer.echo(f"  skipped {count}: {why}")


@evals_app.command("capture")
def evals_capture(
    trace_id: str = typer.Argument(..., help="an eval step id or run id from the step log"),
    file: str = typer.Option("captured.yaml", "--file", help="golden file to append to"),
) -> None:
    """Write a golden case from a trace: inputs, observed output, and its replay material."""
    settings = _settings()
    try:
        case, path = capture.capture(
            steps.log_path(), trace_id, settings.ccp_golden_dir, filename=file
        )
    except LookupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"captured {case.id} -> {path}")


@evals_app.command("threshold")
def evals_threshold(
    apply: bool = typer.Option(False, "--apply", help="write it to config/evaluation.yaml"),
    allow_lower: bool = typer.Option(
        False, "--allow-lower", help="let --apply lower the threshold below the current one"
    ),
) -> None:
    """The interrupt threshold the last run's reliability curve suggests."""
    settings = _settings()
    latest = settings.ccp_eval_runs_dir / "latest.json"
    if not latest.exists():
        typer.echo("no evals/runs/latest.json; run `ccp evals run` first", err=True)
        raise typer.Exit(code=2)
    report = regress.load_report(latest)
    typer.echo(f"current interrupt_threshold: {thresholds.load().interrupt_threshold}")
    suggested = report.metrics.suggested_interrupt_threshold if report.metrics else None
    if suggested is None:
        typer.echo("the reliability curve has too few points to suggest a threshold; kept")
        return
    typer.echo(f"suggested from the curve: {suggested:.2f}")
    if apply:
        current = thresholds.load().interrupt_threshold
        if suggested < current and not allow_lower:
            typer.echo(
                "the suggestion would lower the human gate; pass --allow-lower to apply it",
                err=True,
            )
            raise typer.Exit(code=1)
        path = eval_runner.write_threshold(thresholds.CONFIG_PATH, suggested)
        typer.echo(f"written to {path}")


@app.command("replay")
def replay_record(
    record_id: str = typer.Argument(
        ..., help="a verdict, investigation, gap report, or copilot answer id"
    ),
    tenant: str | None = typer.Option(None, "--tenant"),
    execute: bool = typer.Option(
        False, "--execute", help="re-send the stored request and diff the response"
    ),
) -> None:
    """Rebuild a stored model request from its references and diff it against what was sent.
    The request diff must be empty; a re-executed response is exact for the stub client and
    only approximate for a live model."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        try:
            report = replay.rebuild(scoped, settings, record_id)
        except LookupError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(report.format())
        if execute:
            llm = try_build_client(settings)
            if llm is None:
                typer.echo("--execute needs ANTHROPIC_API_KEY", err=True)
                raise typer.Exit(code=2)
            for name, diff in replay.execute(scoped, settings, record_id, llm):
                outcome = "response identical" if not diff else "; ".join(diff)
                typer.echo(f"  re-executed {name}: {outcome}")
        if report.diff:
            raise typer.Exit(code=1)
    finally:
        pool.close()


def _settings() -> Settings:
    settings = Settings()
    steps.configure(settings)
    steps.new_run()  # one run id per command: one trace, one capture handle
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


@db_app.command("lint")
def db_lint() -> None:
    """The row-level security catalogue lint (A-99). Exit 1 on any finding."""
    findings = db.lint_catalogue(_settings())
    for finding in findings:
        typer.echo(str(finding))
    if findings:
        raise typer.Exit(1)
    typer.echo("every tenant table is forced under its tenant policy and classified")
