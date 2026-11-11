"""The CLI: ``python -m ccp ...`` or ``ccp ...`` after ``uv sync``."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from ccp import auth
from ccp.agent import cli as agent_cli
from ccp.agent import tier3 as _tier3  # noqa: F401 - registers tier 3
from ccp.approvals import actions as approval_actions
from ccp.approvals import cli as approvals_cli
from ccp.audit import checkpoints as audit_checkpoints
from ccp.audit import verify as audit_verify
from ccp.auth import members
from ccp.compliance_state import reader as state_reader
from ccp.compliance_state import run as pipeline
from ccp.compliance_state import writer as state_writer  # noqa: F401 - registers the state consumer
from ccp.config import Settings
from ccp.connectors.aws import onboarding as aws_onboarding
from ccp.connectors.base import parse_ts
from ccp.connectors.registry import CONNECTOR_NAMES, SOURCE_KINDS, build_connector
from ccp.copilot import cli as copilot_cli
from ccp.evals import calibration as judge_calibration
from ccp.evals import capture, regress, shadow, sizing
from ccp.evals import drafting as check_drafting
from ccp.evals import labels as eval_labels
from ccp.evals import mapping as mapping_cli
from ccp.evals import production as _production  # noqa: F401 - registers the quality jobs
from ccp.evals import relabel as eval_relabel
from ccp.evals import runner as eval_runner
from ccp.evals.schema import GoldenCase
from ccp.evaluators import clock as clock_producer
from ccp.evaluators import (
    evidence_cli,  # registers upload feedback and adequacy jobs
    inventory,
    rederive,
    rule_changes,
    thresholds,
    tier2,
)
from ccp.evaluators import impact as impact_step
from ccp.evaluators import jobs as _jobs  # noqa: F401 - registers jobs and the scheduler
from ccp.evaluators import quality as eval_quality
from ccp.evaluators import runner as evaluations
from ccp.evaluators import verdict as verdicts
from ccp.identity import resolver
from ccp.ingest import aws_accounts, raw_audit, worker
from ccp.ingest import ops as queue_ops
from ccp.ingest.landing import ingest_tenant
from ccp.investigation import cli as investigate_cli
from ccp.knowledge import graph as knowledge_graph
from ccp.knowledge import rollup as knowledge_rollup
from ccp.llm import release
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import try_build_client
from ccp.lookups import cli as lookups_cli
from ccp.lookups import runner as _lookups  # noqa: F401 - registers the lookup consumers
from ccp.normalize import consumer as normalize_consumer
from ccp.normalize import repository
from ccp.observability import steps
from ccp.policy import cli as policy_cli
from ccp.questionnaires import cli as questionnaires_cli
from ccp.questionnaires import send as _answers  # noqa: F401 - registers the staleness job
from ccp.remediation import cli as remediate_cli
from ccp.remediation import fix_cli
from ccp.replay import rebuild as replay
from ccp.retrieval import calibration, distractors, generations, reembed
from ccp.retrieval import config as retrieval_cfg
from ccp.retrieval import index as retrieval_index
from ccp.retrieval import metrics as retrieval_metrics
from ccp.retrieval.embeddings import EmbeddingModelUnavailable, embedder_for
from ccp.retrieval.hybrid import retrieve
from ccp.storage import bitemporal, db
from ccp.storage import exceptions as stored_exceptions
from ccp.storage.scoped import ScopedDB
from ccp.tasks import cli as tasks_cli  # registers the tasks job too
from ccp.tenancy import TenantContext
from ccp.tenants import onboarding
from ccp.tenants import scope as tenant_scope
from ccp.tenants import versions as tenant_versions

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
connections_app = typer.Typer(no_args_is_help=True, help="Connections and their health (A-121)")
auth_app = typer.Typer(no_args_is_help=True, help="Sign-in tokens (A-129)")
members_app = typer.Typer(no_args_is_help=True, help="Roles per tenant (A-129)")
audit_app = typer.Typer(no_args_is_help=True, help="Check the record against itself (MT5)")
bundles_app = typer.Typer(no_args_is_help=True, help="Release bundles (A-142)")
aws_app = typer.Typer(no_args_is_help=True, help="Onboard AWS accounts to a read-only role (A-139)")
rules_app = typer.Typer(
    no_args_is_help=True, help="Rule changes: amendments by default, corrections by decision"
)
app.add_typer(knowledge_app, name="knowledge")
knowledge_app.add_typer(mapping_cli.map_app, name="map")
knowledge_app.add_typer(check_drafting.app, name="checks")
app.add_typer(tenants_app, name="tenants")
app.add_typer(evaluate_app, name="evaluate")
app.add_typer(index_app, name="index")
app.add_typer(agent_cli.app, name="agent")
app.add_typer(approvals_cli.app, name="approvals")
app.add_typer(approvals_cli.exceptions_app, name="exceptions")
app.add_typer(investigate_cli.app, name="investigate")
app.add_typer(policy_cli.app, name="policy")
app.command("copilot")(copilot_cli.copilot)
app.add_typer(remediate_cli.app, name="remediate")
app.add_typer(fix_cli.app, name="fixes")
app.add_typer(evidence_cli.app, name="evidence")
app.add_typer(questionnaires_cli.app, name="questionnaires")
app.add_typer(tasks_cli.app, name="tasks")
app.add_typer(lookups_cli.app, name="lookups")
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
        held = stored_exceptions.excepted_until(scoped)
    finally:
        pool.close()
    if not rows:
        typer.echo("NO EVALUATIONS YET" if not subject else f"no state for {subject}")
    for row in rows:
        typer.echo(state_reader.format_row(row, held.get(row.subject_key)))


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
    curves = calibration.reliability_curves(report)
    for key, curve in sorted(curves.items()):
        typer.echo(f"reliability {key} over {curve.samples} queries: {len(curve.bins)} bins")
    if write:
        updated = calibration.record(providers.retrieval, cal)
        retrieval_cfg.save(calibration.record_reliability(updated, curves))
        typer.echo(f"wrote {retrieval_cfg.CONFIG_PATH}")


@index_app.command("compare")
def index_compare(
    embed: str = typer.Option(
        ..., "--embed", help="candidate embedding model, e.g. voyage-4-large"
    ),
    rerank: str = typer.Option(..., "--rerank", help="candidate reranker, e.g. rerank-3, or none"),
    baseline_rerank: str | None = typer.Option(
        None, "--baseline-rerank", help="default: the configured reranker"
    ),
) -> None:
    """Paired comparison of a candidate pair against the serving one on every tenant's golden
    queries (A-149). Writes the candidate generation beside the serving one; never switches."""
    from ccp.retrieval import compare as paired
    from ccp.retrieval.rerank import RerankModelUnavailable, reranker_for

    settings = _settings()
    providers = tier2.Providers.from_settings(settings)
    queries = retrieval_metrics.load_queries()
    pool = db.app_pool(settings)
    runs = []
    usd = 0.0
    try:
        for tenant_id in sorted({q.tenant for q in queries}):
            scoped = ScopedDB(pool, _tenant(settings, tenant_id))
            serving = generations.active(scoped)
            if serving is None:
                typer.echo(f"{tenant_id}: no index yet; run `ccp index refresh`", err=True)
                raise typer.Exit(2)
            try:
                baseline = paired.Side(
                    embedder_for(settings, serving.embed_model),
                    reranker_for(settings, baseline_rerank)
                    if baseline_rerank is not None
                    else providers.reranker,
                    serving.generation_id,
                )
                candidate_embedder = embedder_for(settings, embed)
                candidate_reranker = reranker_for(settings, rerank)
            except (EmbeddingModelUnavailable, RerankModelUnavailable) as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(2) from exc
            built = retrieval_index.refresh(scoped, settings, candidate_embedder, providers.llm)
            usd += built.usd
            candidate = paired.Side(candidate_embedder, candidate_reranker, built.generation_id)
            runs.append(paired.compare(scoped, queries, baseline, candidate, providers.retrieval))
    finally:
        pool.close()
    summary = paired.merge(runs).summary()
    summary["candidate_index_usd"] = round(usd, 6)
    for key in (
        "baseline",
        "candidate",
        "queries",
        "baseline_mrr",
        "candidate_mrr",
        "mrr_delta",
        "mrr_delta_ci95",
        "recall_at_5_delta",
        "recall_at_5_delta_ci95",
        "wins",
        "losses",
        "ties",
        "sign_test_p",
        "latency_ms_p50",
        "candidate_index_usd",
        "verdict",
    ):
        typer.echo(f"{key}: {summary[key]}")
    for etype, row in summary["by_type"].items():
        typer.echo(f"  {etype}: {row}")
    out = settings.ccp_eval_runs_dir / (
        f"retrieval-compare-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    typer.echo(f"wrote {out}")


@index_app.command("gate")
def index_gate(
    distractor_count: int = typer.Option(300, "--distractors", help="installed where none are"),
) -> None:
    """Per evidence type retrieval gates over every tenant's golden queries (A-148)."""
    from ccp.retrieval import gates as retrieval_gates

    settings = _settings()
    providers = tier2.Providers.from_settings(settings)
    queries = retrieval_metrics.load_queries()
    pool = db.app_pool(settings)
    runs = []
    try:
        for tenant_id in sorted({q.tenant for q in queries}):
            scoped = ScopedDB(pool, _tenant(settings, tenant_id))
            if retrieval_index.stats(scoped)["distractors"] == 0:
                distractors.install(scoped, providers.embedder, distractor_count)
            runs.append(
                retrieval_metrics.evaluate_queries(
                    scoped,
                    providers.embedder,
                    queries,
                    reranker=providers.reranker,
                    config=providers.retrieval,
                )
            )
    finally:
        pool.close()
    report = retrieval_metrics.merge(runs)
    outcomes = retrieval_gates.check(report)
    for o in outcomes:
        m = o.metrics
        state = (
            ("FAIL " + "; ".join(o.breaches)) if o.breaches else ("ok" if o.gated else "ungated")
        )
        typer.echo(
            f"{o.evidence_type:<24} n={m.queries:<3} recall@5={m.recall_at_5:.3f}"
            f" mrr={m.mrr:.3f} distractor@1={m.distractor_at_1:.3f}  {state}"
        )
    reasons = retrieval_gates.blocking(report, outcomes)
    for reason in reasons:
        typer.echo(f"blocked: {reason}", err=True)
    if reasons:
        raise typer.Exit(1)


@index_app.command("generations")
def index_generations(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Every index generation (embedding model x chunker), its sets and versions, which serves."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        rows = generations.history(ScopedDB(pool, ctx))
    finally:
        pool.close()
    if not rows:
        typer.echo("no generations: run `ccp index refresh`")
    for r in rows:
        mark = "*" if r["active"] else " "
        typer.echo(
            f"{mark} {r['generation_id']} {r['embed_model']} chunker={r['chunker_version']}"
            f" sets={r['sets']} chunk_versions={r['chunk_versions']}"
            f" last_activated={r['last_activated_at']}"
        )


@index_app.command("reembed")
def index_reembed(
    model: str = typer.Option(..., "--model", help="the candidate embedding model id"),
    tenant: str | None = typer.Option(None, "--tenant"),
    switch: bool = typer.Option(True, "--switch/--no-switch", help="switch when the gate passes"),
    force: bool = typer.Option(False, "--force", help="switch even when the gate fails"),
) -> None:
    """Embed into a new generation beside the serving one; switch only after the gate."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    providers = tier2.Providers.from_settings(settings)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        serving = generations.active(scoped)
        try:
            candidate = embedder_for(settings, model)
            serving_embedder = embedder_for(settings, serving.embed_model) if serving else None
        except EmbeddingModelUnavailable as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from exc
        result = reembed.reembed(
            scoped,
            settings,
            candidate,
            serving_embedder,
            llm=providers.llm,
            switch=switch,
            force=force,
        )
    finally:
        pool.close()
    typer.echo(
        f"candidate={result.candidate_generation} serving_before={result.serving_generation}"
        f" sets_written={result.refresh.sets_written} embedded={result.refresh.embedded}"
        f" usd={result.refresh.usd:.4f}"
    )
    if result.gate is not None:
        typer.echo(f"gate passed={result.gate.passed} candidate={result.gate.candidate}")
        typer.echo(f"serving={result.gate.serving}")
        for reason in result.gate.reasons:
            typer.echo(f"  {reason}")
    for event in result.events:
        typer.echo(f"event {event}", err=True)
    typer.echo(f"switched={result.switched} projected={result.projected}")
    if result.gate is not None and not result.gate.passed and not force:
        raise typer.Exit(1)


@index_app.command("search")
def index_search(
    query: str = typer.Argument(..., help="the retrieval query"),
    evidence_type: str = typer.Option(..., "--type"),
    tenant: str | None = typer.Option(None, "--tenant"),
    subject: str | None = typer.Option(None, "--subject"),
    as_of: datetime | None = typer.Option(
        None, "--as-of", help="search what the index held then (versioned chunks)"
    ),
    generation: str | None = typer.Option(None, "--generation", help="a named generation"),
) -> None:
    """Run the hybrid query and print the candidates with both ranks and the fused score."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    providers = tier2.Providers.from_settings(settings)
    pool = db.app_pool(settings)
    when = as_of.replace(tzinfo=UTC) if as_of is not None and as_of.tzinfo is None else as_of
    try:
        result = retrieve(
            ScopedDB(pool, ctx),
            providers.embedder,
            query,
            evidence_type=evidence_type,
            subject_entity_id=subject,
            reranker=providers.reranker,
            config=providers.retrieval,
            as_of=when,
            generation_id=generation,
        )
    finally:
        pool.close()
    if result.generation_id is not None:
        typer.echo(f"generation {result.generation_id} as of {result.as_of or 'now'}", err=True)
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
        scoped = ScopedDB(pool, ctx)
        heads = bitemporal.heads(scoped)
        covered = inventory.coverage(scoped)  # expected subjects and parked ones (A-124)
        held = stored_exceptions.excepted_until(scoped)  # fail (excepted until D) (A-132)
    finally:
        pool.close()
    result = knowledge_rollup.rollup(graph, heads, framework, covered, held)
    typer.echo(knowledge_rollup.format_rollup(result))


app.add_typer(db_app, name="db")
app.add_typer(connectors_app, name="connectors")
app.add_typer(ingest_app, name="ingest")
app.add_typer(connections_app, name="connections")
app.add_typer(rules_app, name="rules")
app.add_typer(auth_app, name="auth")
app.add_typer(members_app, name="members")
app.add_typer(audit_app, name="audit")
app.add_typer(aws_app, name="aws")
app.add_typer(bundles_app, name="bundles")
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
    token: str | None = typer.Option(None, "--token", envvar="CCP_TOKEN"),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """List open review items with the approval item that decides each, or decide one as
    merge or distinct, signed in as an approver (A-132)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        items_for = resolver.ensure_review_items(scoped)
        if resolve:
            if outcome not in ("merge", "distinct") or not token:
                raise typer.BadParameter("--resolve needs --as merge|distinct and --token")
            try:
                who = auth.authenticate(settings, token)
            except auth.InvalidToken as exc:
                raise typer.BadParameter(f"sign-in token refused: {exc}") from exc
            approval_id = items_for.get(resolve)
            if approval_id is None:
                raise typer.BadParameter(f"no open review item {resolve!r}")
            try:
                if outcome == "merge":
                    done = approval_actions.approve(scoped, settings, approval_id, who, note)
                else:
                    done = approval_actions.reject(
                        scoped, settings, approval_id, who, note or "kept as two people"
                    )
            except (LookupError, ValueError, PermissionError) as exc:
                raise typer.BadParameter(f"refused: {exc}") from exc
            typer.echo(f"{resolve}: {outcome} by {who.label}; {done.detail}")
            return
        items = resolver.open_reviews(scoped)
        if not items:
            typer.echo("no open review items")
        for item in items:
            typer.echo(
                f"{item.decision_id}  approval={items_for.get(item.decision_id)}"
                f"  score={item.score}  {item.left_entity_id}  <->  {item.right_entity_id}"
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
    for run in summary.runs:  # every pull is a sync run (A-118)
        partial = [f"{s}={o.status}" for s, o in run.scopes.items() if o.status != "complete"]
        tail = f"  ({', '.join(partial)})" if partial else ""
        typer.echo(f"{run.connector:<8} {run.status:<8} landed {run.landed}{tail}")
        if run.error:
            typer.echo(f"         {run.error}", err=True)
    typer.echo(f"total landed {summary.landed}, duplicates {summary.duplicates}")
    if summary.failed:
        raise typer.Exit(1)


@connections_app.command("list")
def connections_list(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """The tenant's connections, each with its health, from its checks and sync runs."""
    from ccp.ingest import connections

    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        connections.sync(scoped)
        found = [connections.health(scoped, settings, c) for c in connections.current(scoped)]
    finally:
        pool.close()
    for h in found:
        c = h.connection
        typer.echo(
            f"{c.connection_id:<32} {c.source:<8} {c.account:<20} v{c.version}"
            f"  {h.state:<12} {h.reason}"
        )


@connections_app.command("check")
def connections_check(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Probe every enabled connection's credential now, as the daily job does."""
    from ccp.ingest import connections

    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        checks = connections.check_all(settings, ScopedDB(pool, ctx))
    finally:
        pool.close()
    for c in checks:
        typer.echo(f"{c.connection_id:<32} {c.outcome:<12} {c.detail}")
    if any(c.outcome != "ok" for c in checks):
        raise typer.Exit(1)


@ingest_app.command("transcribe")
def ingest_transcribe(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Transcribe scanned pages and screenshots with Claude, then land the text (A-147)."""
    from ccp.retrieval import transcribe

    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        summary = transcribe.transcribe(ScopedDB(pool, ctx), settings)
    finally:
        pool.close()
    typer.echo(
        f"documents={summary.documents} pages={summary.pages} illegible={summary.illegible}"
        f" landed={summary.landed} usd={summary.usd:.4f}"
    )
    for event in summary.events:
        typer.echo(f"event {event}", err=True)
    typer.echo("then `ccp normalize run` maps the transcribed text into evidence", err=True)


@ingest_app.command("runs")
def ingest_runs(
    tenant: str | None = typer.Option(None, "--tenant"),
    limit: int = typer.Option(10, "--limit"),
) -> None:
    """The tenant's latest sync runs, newest first, each scope's outcome (A-118)."""
    from ccp.ingest import sync_runs

    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        runs = sync_runs.recent(ScopedDB(pool, ctx), limit)
    finally:
        pool.close()
    for run in runs:
        typer.echo(
            f"{run['finished_at'].isoformat()} {run['connector']:<8} {run['source']:<8}"
            f" {run['status']:<8} landed {run['landed']}"
            + (f"  {run['error_kind']}: {run['error']}" if run["error"] else "")
        )
        for scope, outcome in run["scopes"].items():
            detail = f"  {outcome['detail']}" if outcome["detail"] else ""
            typer.echo(f"    {scope:<16} {outcome['status']:<12} {outcome['items']}{detail}")


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


@evals_app.command("size")
def evals_size(
    bound: float = typer.Option(0.02, "--bound", help="the false-PASS rate to bound"),
    confidence: float = typer.Option(0.95, "--confidence"),
) -> None:
    """Whether each AI target's gold set can bound its false-PASS rate (A-144). Exits 1 when
    one cannot."""
    settings = _settings()
    cases, _ = eval_runner.load_cases(settings.ccp_golden_dir)
    found = sizing.size(cases, bound, confidence)
    typer.echo(
        f"{sizing.fail_cases_needed(bound, confidence)} FAIL cases bound false PASS at"
        f" {bound:.0%} with {confidence:.0%} confidence"
    )
    for t in found:
        status = "; ".join(t.findings) or "ok"
        typer.echo(f"{t.target:<14} {t.fail_cases:>4}/{t.cases:<4} FAIL ({t.share:.0%})  {status}")
    if any(t.findings for t in found):
        raise typer.Exit(1)


@evals_app.command("shadow")
def evals_shadow(
    model: str = typer.Option(..., "--model", help="the candidate tier-2 model"),
    tests: list[str] = typer.Option([], "--test"),
    limit: int = typer.Option(50, "--limit"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Re-send stored tier-2 requests to a candidate model and diff what the rule decides
    (A-143). Writes no verdict; exits 1 when any status would flip to pass."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    llm = try_build_client(settings)
    if llm is None:
        raise typer.BadParameter("ANTHROPIC_API_KEY is not set: nothing to run the candidate on")
    pool = db.app_pool(settings)
    try:
        report = shadow.run(
            ScopedDB(pool, ctx), settings, llm, model, tests=tuple(tests), limit=limit
        )
    finally:
        pool.close()
    for flip in report.flips:
        typer.echo(
            f"{flip.test_id} {flip.subject_key}: {flip.stored} -> {flip.candidate}"
            f" ({', '.join(flip.fields)})"
        )
    typer.echo(
        f"compared {report.compared}, skipped {report.skipped}, flips {len(report.flips)},"
        f" towards pass {len(report.towards_pass)}"
    )
    if not report.passed:
        raise typer.Exit(1)


@evals_app.command("refusals")
def evals_refusals(
    days: int = typer.Option(7, "--days"), tenant: str | None = typer.Option(None, "--tenant")
) -> None:
    """Each test's model refusal rate over the window (A-143)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        rates = eval_quality.refusal_rates(
            ScopedDB(pool, ctx), datetime.now(UTC) - timedelta(days=days)
        )
    finally:
        pool.close()
    if not rates:
        typer.echo("no AI evaluations in the window")
    for r in rates:
        typer.echo(f"{r.test_id:<6} {r.refusals}/{r.attempts} refused ({r.rate:.1%})")


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
    agreement = regress.latest_judge_agreement(settings.ccp_eval_runs_dir)
    reg = regress.diff(base, report, strict=strict, gates=gates, judge_agreement=agreement)
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


@evals_app.command("relabel")
def evals_relabel(
    month: str = typer.Option(..., "--month", help="YYYY-MM: the batch to show or check"),
    item: str | None = typer.Option(None, "--item", help="a judged item to label"),
    passed: bool | None = typer.Option(None, "--pass/--fail", help="the expert's label"),
    by: str | None = typer.Option(None, "--by"),
    note: str = typer.Option("", "--note"),
    check: bool = typer.Option(False, "--check", help="agreement against the calibration"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """The monthly expert relabel (A-157): list the month's 20 judged items, label one, or
    check the month's expert-judge agreement against the judge's calibrated interval."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        if item is not None:
            if passed is None or not by:
                raise typer.BadParameter("labelling needs --pass or --fail, and --by")
            eval_relabel.label(scoped, item, passed=passed, by=by, note=note)
        if check:
            out = eval_relabel.check(
                scoped, month, settings.ccp_eval_runs_dir, release.bundle_id(settings)
            )
            typer.echo(json.dumps(out, indent=2, sort_keys=True, default=str))
            if out.get("recalibrate"):
                raise typer.Exit(code=1)
            return
        for row in eval_relabel.batch(scoped, month):
            typer.echo(json.dumps(row, sort_keys=True, default=str))
    finally:
        pool.close()


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


@auth_app.command("dev-token")
def auth_dev_token(
    subject: str = typer.Option(..., "--sub", help="the person's stable id at the issuer"),
    email: str | None = typer.Option(None, "--email"),
    name: str | None = typer.Option(None, "--name"),
    hours: int = typer.Option(8, "--hours"),
) -> None:
    """A token from the local dev issuer, for CCP_AUTH_MODE=dev only (A-129)."""
    settings = _settings()
    if settings.ccp_auth_mode != "dev":
        typer.echo("dev tokens are for CCP_AUTH_MODE=dev only", err=True)
        raise typer.Exit(code=2)
    from ccp.auth import dev_issuer

    typer.echo(dev_issuer.mint(settings, subject, email, name, timedelta(hours=hours)))


@members_app.command("list")
def members_list(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """The tenant's members and their roles (A-129)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        found = members.current(ScopedDB(pool, ctx))
    finally:
        pool.close()
    for m in found:
        typer.echo(f"{m.role:<9} {m.principal.label:<28} {m.principal.principal_id}")


@members_app.command("grant")
def members_grant(
    subject: str = typer.Option(..., "--sub", help="the person's stable id at the issuer"),
    role: str = typer.Option(..., "--role", help="viewer | reviewer | approver | admin"),
    issuer: str | None = typer.Option(None, "--issuer", help="default: the trusted issuer"),
    email: str | None = typer.Option(None, "--email"),
    name: str | None = typer.Option(None, "--name"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Grant a role from the server's command line, recorded as granted by the operator; a
    signed-in admin grants through the API instead (A-129)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    chosen = next((r for r in members.ROLES if r == role), None)
    if chosen is None:
        raise typer.BadParameter(f"role must be one of {', '.join(members.ROLES)}")
    who = auth.Principal(issuer or auth.verifier(settings).issuer, subject, email, name)
    pool = db.app_pool(settings)
    try:
        granted = members.grant(ScopedDB(pool, ctx), who, chosen, members.OPERATOR)
    finally:
        pool.close()
    typer.echo(f"{granted.role} {who.label} ({who.principal_id}) in {ctx.tenant_id}")


@members_app.command("revoke")
def members_revoke(
    subject: str = typer.Option(..., "--sub"),
    issuer: str | None = typer.Option(None, "--issuer", help="default: the trusted issuer"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """End a membership, recorded as revoked by the operator (A-129)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    who = auth.Principal(issuer or auth.verifier(settings).issuer, subject)
    pool = db.app_pool(settings)
    try:
        members.revoke(ScopedDB(pool, ctx), who, members.OPERATOR)
    except LookupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    finally:
        pool.close()
    typer.echo(f"revoked {who.principal_id} in {ctx.tenant_id}")


@rules_app.command("preview")
def rules_preview(
    test_id: str = typer.Argument(..., help="a test id, such as GH-2"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Each subject's last verdict under an older rule, and what the current rule decides
    from the same inputs (A-128). A * marks a subject whose status would change."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        shown = rule_changes.preview(ScopedDB(pool, ctx), settings, test_id)
    finally:
        pool.close()
    typer.echo(f"{test_id}: rule {', '.join(shown.from_stamps) or 'none'} -> {shown.to_stamp}")
    for flip in shown.subjects:
        mark = "*" if flip.before != flip.after else " "
        typer.echo(f"{mark} {flip.subject_key}: {flip.before} -> {flip.after}")


@rules_app.command("correct")
def rules_correct(
    test_id: str = typer.Argument(..., help="a test id, such as GH-2"),
    by: str = typer.Option(..., "--by", help="who decides the old rule was wrong"),
    signed_off_by: str | None = typer.Option(
        None, "--signed-off-by", help="needed when a FAIL would become a PASS"
    ),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Re-decide the test's past under its current rule, from the inputs each verdict read,
    and record the notice (A-128)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        try:
            done = rule_changes.correct(scoped, settings, test_id, by, signed_off_by)
        except (rule_changes.SignOffRequired, LookupError, ValueError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        state_writer.drain(scoped)
    finally:
        pool.close()
    typer.echo(done.notice)


@rules_app.command("notices")
def rules_notices(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Every rule change recorded for the tenant, oldest first (A-128)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        rows = ScopedDB(pool, ctx).fetch_all(
            "SELECT decided_at, kind, test_id, notice FROM rule_changes ORDER BY decided_at"
        )
    finally:
        pool.close()
    for decided_at, kind, test_id, notice in rows:
        typer.echo(f"{decided_at.isoformat()} {kind:<10} {test_id}")
        typer.echo(f"    {notice}")


@evaluate_app.command("sentinel")
def evaluate_sentinel(
    tenant: str | None = typer.Option(None, "--tenant"),
    sample: int = typer.Option(25, "--sample", help="subjects whose newest verdict is re-derived"),
) -> None:
    """Re-derive the newest tier-1 verdict of up to SAMPLE subjects from what each read, as
    the nightly sentinel does (A-125). Exits 1 on any mismatch."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        results = rederive.sentinel(ScopedDB(pool, ctx), settings, sample)
    finally:
        pool.close()
    for r in results:
        typer.echo(f"{r.outcome:<13} {r.subject_key}  {r.verdict_id}")
        for line in r.diff:
            typer.echo(f"    {line}")
    if any(r.outcome == "mismatch" for r in results):
        raise typer.Exit(1)


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
        stored = verdicts.load(scoped, record_id)
        if stored is not None and stored.tier == 1:  # re-derived from what it read (A-125)
            try:
                result = rederive.rederive(scoped, settings, stored)
            except LookupError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=2) from exc
            typer.echo(
                f"verdict {record_id} {result.test_id} {result.subject_key}: {result.outcome}"
            )
            for line in result.diff:
                typer.echo(f"  {line}")
            if result.outcome == "mismatch":
                raise typer.Exit(code=1)
            return
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


@connectors_app.command("conformance")
def connectors_conformance() -> None:
    """Every live connector's recorded scenarios, offline, negative controls included
    (A-122). Exits 1 when a scenario fails or a control goes uncaught."""
    from ccp.connectors import conformance

    suites, controls = conformance.default()
    report = conformance.run(suites, controls)
    for result in report.results:
        typer.echo(f"{'ok  ' if result.passed else 'FAIL'} {result.suite:<7} {result.scenario}")
        for problem in result.problems:
            typer.echo(f"         {problem}")
    for control, results in report.controls:
        caught = sum(1 for r in results if not r.passed)
        mark = "ok  " if caught else "BLIND"
        typer.echo(f"{mark} control {control.name}: caught by {caught} scenario(s)")
    if not report.green:
        raise typer.Exit(1)


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


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Serve the HTTP API (A-134): every request signed in, per tenant, by role."""
    import uvicorn

    from ccp.api.app import create_app

    settings = _settings()
    typer.echo(f"serving on http://{host}:{port} (sign-in: {settings.ccp_auth_mode})")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


@bundles_app.command("show")
def bundles_show() -> None:
    """The bundle this process runs under, and its manifest."""
    settings = _settings()
    identifier = release.bundle_id(settings)
    typer.echo(f"bundle {identifier}")
    typer.echo(json.dumps(release.manifest_of(identifier), indent=2, sort_keys=True))


@bundles_app.command("diff")
def bundles_diff(
    before: str, after: str, tenant: str | None = typer.Option(None, "--tenant")
) -> None:
    """What changed between two recorded bundles."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        rows = dict(
            ScopedDB(pool, ctx).fetch_all(
                "SELECT bundle_id, manifest FROM release_bundles WHERE bundle_id = ANY(%s)",
                ([before, after],),
            )
        )
    finally:
        pool.close()
    missing = [b for b in (before, after) if b not in rows]
    if missing:
        raise typer.BadParameter(f"no recorded bundle {', '.join(missing)}")
    for line in release.differences(rows[before], rows[after]) or ["no differences"]:
        typer.echo(line)


@bundles_app.command("coverage")
def bundles_coverage(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Verdicts and agent runs per bundle; any that names none is listed (A-142)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        verdict_rows = scoped.fetch_all(
            "SELECT coalesce(bundle_id, '(none)'), count(*) FROM verdicts GROUP BY 1 ORDER BY 1"
        )
        run_rows = scoped.fetch_all(
            "SELECT coalesce(bundle_id, '(none)'), count(*) FROM agent_threads GROUP BY 1"
            " ORDER BY 1"
        )
    finally:
        pool.close()
    for label, rows in (("verdicts", verdict_rows), ("agent runs", run_rows)):
        for bundle, n in rows:
            typer.echo(f"{label:<11} {bundle:<17} {n}")
    if any(b == "(none)" for b, _ in [*verdict_rows, *run_rows]):
        raise typer.Exit(1)


@aws_app.command("onboard")
def aws_onboard(
    account: str = typer.Option(..., "--account", help="the 12-digit AWS account id"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Generate the account's external ID and print the quick-create link for the role."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        found = aws_accounts.start(ScopedDB(pool, ctx), account, members.OPERATOR)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        pool.close()
    typer.echo(f"account {found.account_id}: {found.status}; external ID {found.external_id}")
    typer.echo(
        aws_onboarding.quick_create_url(
            settings.ccp_aws_template_url,
            found.external_id,
            settings.ccp_aws_principal_arn,
            settings.aws_region,
        )
    )
    typer.echo("deploy the stack, then: ccp aws verify --account ... --role-arn <its RoleArn>")


@aws_app.command("verify")
def aws_verify(
    account: str = typer.Option(..., "--account"),
    role_arn: str = typer.Option(..., "--role-arn"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Check the role before anything is read: it works with CCP's external ID, fails without
    it, and can neither write nor read a secret. Exits 1 when it is refused."""
    import boto3

    settings = _settings()
    ctx = _tenant(settings, tenant)
    make: Any = boto3.client
    pool = db.app_pool(settings)
    try:
        scoped = ScopedDB(pool, ctx)
        started = aws_accounts.current(scoped, account)
        if started is None:
            raise typer.BadParameter(f"run `ccp aws onboard --account {account}` first")
        result = aws_onboarding.verify(
            account,
            role_arn,
            started.external_id,
            make("sts", region_name=settings.aws_region),
            lambda creds: make(
                "iam",
                region_name=settings.aws_region,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            ),
        )
        recorded = aws_accounts.record(scoped, result, members.OPERATOR)
    finally:
        pool.close()
    for finding in recorded.findings:
        typer.echo(f"refused: {finding}")
    typer.echo(f"account {account}: {recorded.status}")
    if recorded.status != "verified":
        raise typer.Exit(1)
    typer.echo("add the connection: connector aws, source live, account " + account)


@audit_app.command("verify")
def audit_verify_cmd(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Walk the tenant's chain, re-hash every chained row, check every checkpoint (A-137).
    Exits 1 on any finding."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        report = audit_verify.verify(ScopedDB(pool, ctx), settings)
    finally:
        pool.close()
    for finding in report.findings:
        typer.echo(finding)
    typer.echo(
        f"{report.entries} entries, {report.rows} rows, {report.checkpoints} checkpoints;"
        f" {len(report.findings)} findings"
    )
    if not report.clean:
        raise typer.Exit(1)


@audit_app.command("checkpoint")
def audit_checkpoint_cmd(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Sign and export the tenant's chain head now (the hourly job does it too)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        made = audit_checkpoints.checkpoint(ScopedDB(pool, ctx), settings)
    finally:
        pool.close()
    if made is None:
        typer.echo("the chain has not moved since the last checkpoint")
    else:
        typer.echo(f"checkpoint at entry {made.seq}, signed by {made.key_id}: {made.exported_to}")


@audit_app.command("raw")
def audit_raw(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """Re-read every raw object by its pointer and hash it (A-136). Exits 1 on any object
    missing or altered."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        found = raw_audit.verify(ScopedDB(pool, ctx), settings)
    finally:
        pool.close()
    for problem in found.missing:
        typer.echo(f"missing: {problem}")
    for problem in found.altered:
        typer.echo(f"altered: {problem}")
    typer.echo(
        f"checked {found.checked}; {found.unversioned} rows from before A-136 name no version"
    )
    if not found.clean:
        raise typer.Exit(1)


@tenants_app.command("list")
def tenants_list() -> None:
    """List the active tenants: the tenants file's and those onboarded (A-133)."""
    for ctx in TenantContext.all(_settings()):
        s = ctx.settings
        typer.echo(
            f"{ctx.tenant_id:<8} {s.display_name:<20} github_org={s.github_org} "
            f"admin_limit={s.org_admin_limit} sla_h={s.termination_revocation_sla_hours} "
            f"critical_tags={','.join(s.critical_asset_tags)}"
        )


@tenants_app.command("create")
def tenants_create(
    tenant_id: str,
    display_name: str = typer.Option(..., "--display-name"),
    github_org: str = typer.Option(..., "--github-org"),
    admin_subject: str = typer.Option(..., "--admin-subject", help="the first admin's subject"),
    admin_issuer: str | None = typer.Option(
        None, "--admin-issuer", help="their issuer (default: the configured one)"
    ),
    time_zone: str = typer.Option("UTC", "--time-zone"),
) -> None:
    """Onboard a tenant: its first version and its first admin, no deploy (A-133)."""
    settings = _settings()
    issuer = admin_issuer or auth.verifier(settings).issuer
    pool = db.app_pool(settings)
    try:
        ctx = onboarding.create(
            settings,
            pool,
            tenant_id,
            display_name=display_name,
            github_org=github_org,
            admin=auth.Principal(issuer, admin_subject),
            time_zone=time_zone,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        pool.close()
    typer.echo(f"{ctx.tenant_id}: onboarded; {issuer}#{admin_subject} is its admin")


@tenants_app.command("show")
def tenants_show(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    """The settings in force and the version they come from."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        current, version = tenant_versions.effective(ScopedDB(pool, ctx))
    finally:
        pool.close()
    typer.echo(f"version {version}")
    typer.echo(json.dumps(current.model_dump(mode="json"), indent=2))


@tenants_app.command("history")
def tenants_history(tenant: str | None = typer.Option(None, "--tenant")) -> None:
    settings = _settings()
    ctx = _tenant(settings, tenant)
    pool = db.app_pool(settings)
    try:
        found = tenant_versions.history(ScopedDB(pool, ctx))
    finally:
        pool.close()
    if not found:
        typer.echo("no versions: the tenants file is in force")
    for v in found:
        allowed = f" approval={v.approval_id}" if v.approval_id else ""
        typer.echo(
            f"{v.recorded_at:%Y-%m-%d %H:%M} {v.version_id} {v.status:<10} {v.reason}"
            f"  by {v.recorded_by}{allowed}"
        )


@tenants_app.command("set")
def tenants_set(
    changes: list[str] = typer.Argument(..., help="field=JSON value, e.g. org_admin_limit=2"),
    token: str = typer.Option(..., "--token", envvar="CCP_TOKEN"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Change settings as an admin: a change of scope waits for an approver (A-133)."""
    settings = _settings()
    ctx = _tenant(settings, tenant)
    try:
        who = auth.authenticate(settings, token)
    except auth.InvalidToken as exc:
        raise typer.BadParameter(f"sign-in token refused: {exc}") from exc
    asked: dict[str, object] = {}
    for change in changes:
        field, sep, raw = change.partition("=")
        if not sep:
            raise typer.BadParameter(f"{change!r} is not field=value")
        try:
            asked[field] = json.loads(raw)
        except json.JSONDecodeError:
            asked[field] = raw
    pool = db.app_pool(settings)
    try:
        version, item = tenant_scope.change(ScopedDB(pool, ctx), who, asked)
    except (ValueError, PermissionError) as exc:
        raise typer.BadParameter(f"refused: {exc}") from exc
    finally:
        pool.close()
    if item is not None:
        typer.echo(f"a change of scope: approval {item} waits for an approver")
    else:
        typer.echo(f"recorded as version {version}")


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


api_app = typer.Typer(
    no_args_is_help=True,
    help="The REST surface generated from the tool registry (A-117); served behind sign-in in MT4",
)
app.add_typer(api_app, name="api")


@api_app.command("openapi")
def api_openapi(
    out: Path | None = typer.Option(None, "--out", help="write here, not stdout"),
) -> None:
    """The OpenAPI 3.1 document of the tool routes, generated from the registry."""
    import json

    from ccp.agent import rest

    text = json.dumps(rest.openapi(), indent=2, sort_keys=True)
    if out is None:
        typer.echo(text)
        return
    out.write_text(text + "\n", encoding="utf-8")
    typer.echo(f"wrote {out}")
