"""Eval targets: what a golden case can invoke. Each returns a plain dict to compare.

Targets that need a database get one scratch database per eval run through
``EvalContext.db``; they must not touch the working database.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from ccp.config import Settings
from ccp.connectors.base import ConnectorName, SourceEvent, SourceKind, parse_ts
from ccp.connectors.registry import build_connector
from ccp.ingest import event_log
from ccp.ingest.consumers import OrderedConsumer
from ccp.ingest.event_log import LogRecord
from ccp.ingest.landing import ingest_tenant, land_events
from ccp.ingest.raw_zone import RawZone
from ccp.storage.db import app_pool
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext


@dataclass
class EvalContext:
    settings: Settings
    db_settings: Settings | None = None
    _pool: ConnectionPool | None = field(default=None, repr=False)
    _scratch_dir: Path | None = field(default=None, repr=False)
    _prepared: set[str] = field(default_factory=set, repr=False)
    _indexed: set[str] = field(default_factory=set, repr=False)  # tenants with an index
    _distracted: set[str] = field(default_factory=set, repr=False)  # ...with distractors
    judge_llm: Any = None  # a judge model override (tests); the runner builds one otherwise
    providers: Any = None  # a Providers override (tests); targets build from settings otherwise

    def prepared(self, tenant_id: str | None) -> ScopedDB:
        """A scoped handle on a tenant that has been ingested, normalized, and resolved in
        this run's scratch database (once per tenant per run; the pipeline is idempotent)."""
        from ccp.identity import resolver
        from ccp.normalize.consumer import normalize_all

        db = self.scoped(tenant_id)
        if db.tenant.tenant_id not in self._prepared:
            assert self.db_settings is not None
            run_settings = self.db_settings.model_copy(update={"ccp_data_dir": self.scratch_dir})
            ingest_tenant(run_settings, db, db.tenant)
            normalize_all(db, run_settings)
            resolver.resolve_tenant(db)
            self._prepared.add(db.tenant.tenant_id)
        return db

    @property
    def scratch_dir(self) -> Path:
        """Per-run filesystem scratch space; nothing from a previous run can leak in."""
        if self._scratch_dir is None:
            self._scratch_dir = Path(tempfile.mkdtemp(prefix="ccp-eval-"))
        return self._scratch_dir

    def tenant(self, tenant_id: str | None) -> TenantContext:
        if tenant_id is None:
            raise ValueError("this target needs a tenant")
        return TenantContext.for_id(self.settings, tenant_id)

    def scoped(self, tenant_id: str | None) -> ScopedDB:
        if self.db_settings is None:
            raise RuntimeError("this target needs a database; run with a scratch database")
        if self._pool is None:
            self._pool = app_pool(self.db_settings, max_size=4)
        return ScopedDB(self._pool, self.tenant(tenant_id))

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None
        if self._scratch_dir is not None:
            shutil.rmtree(self._scratch_dir, ignore_errors=True)
            self._scratch_dir = None


TargetFn = Callable[[EvalContext, str | None, dict[str, Any]], dict[str, Any]]
TARGETS: dict[str, TargetFn] = {}


def target(name: str) -> Callable[[TargetFn], TargetFn]:
    def deco(fn: TargetFn) -> TargetFn:
        TARGETS[name] = fn
        return fn

    return deco


def _get_path(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _run_settings(ctx: EvalContext) -> Settings:
    """Settings for code that opens its own connections or writes files (checkpoint pools,
    budgets, drafts): the run's scratch database and scratch directory, never the working
    database the CLI was started against."""
    if ctx.db_settings is None:
        raise RuntimeError("this target needs a database; run with a scratch database")
    return ctx.db_settings.model_copy(update={"ccp_data_dir": ctx.scratch_dir})


def _providers(ctx: EvalContext) -> Any:
    """The AI providers for a target: the context's override (tests) or the configured ones."""
    from ccp.evaluators.tier2 import Providers

    return ctx.providers or Providers.from_settings(ctx.settings)


def _support_precision(verdict: Any) -> float | None:
    """Fraction of extracted values whose cited quote contains the value (spec.md Layer 11);
    None when the verdict keeps no (value, citation) pairs."""
    from ccp.evaluators.citations import value_in_quote

    fields = verdict.extracted_fields or {}
    pairs = supported = 0
    for name, value in fields.items():
        if name.endswith("_citation") or isinstance(value, bool) or value in (None, "", [], {}):
            continue
        citation = fields.get(f"{name}_citation")
        if not isinstance(citation, dict) or not citation.get("quote"):
            continue
        pairs += 1
        supported += int(value_in_quote(value, str(citation["quote"])))
    return round(supported / pairs, 4) if pairs else None


def _verified_citations(citations: Any) -> list[dict[str, Any]]:
    return [
        {"chunk_id": c.chunk_id, "quote": c.quote, "supports": c.supports, "verified": True}
        for c in citations
        if c.verified
    ]


_CONNECTORS: tuple[ConnectorName, ...] = ("github", "hris", "manual")


def _connector_name(value: Any) -> ConnectorName:
    for name in _CONNECTORS:
        if value == name:
            return name
    raise ValueError(f"unknown connector {value!r}")


@target("connector_pull")
def connector_pull(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """Pull one connector: count, counts_by_type, and the events matching `where` (if given)."""
    source: SourceKind = "live" if inp.get("source") == "live" else "fixture"
    connector = build_connector(_connector_name(inp["connector"]), ctx.settings, source)
    since = parse_ts(inp["since"]) if inp.get("since") else None
    events = list(connector.pull(ctx.tenant(tenant_id), since))
    counts: dict[str, int] = {}
    for e in events:
        counts[e.event_type] = counts.get(e.event_type, 0) + 1
    where: dict[str, Any] = inp.get("where") or {}
    matches: list[dict[str, Any]] = []
    if where:
        matches = [
            {"event_type": e.event_type, "source_event_id": e.source_event_id, "payload": e.payload}
            for e in events
            if all(
                _get_path({"event_type": e.event_type, "payload": e.payload}, k) == v
                for k, v in where.items()
            )
        ]
    return {"count": len(events), "counts_by_type": counts, "matches": matches}


@target("ingest_land")
def ingest_land(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """Land one connector's events `times` times into a scratch database and count what happened."""
    db = ctx.scoped(tenant_id)
    zone = RawZone(ctx.scratch_dir / "raw" / db.tenant.tenant_id)
    connector = build_connector(_connector_name(inp["connector"]), ctx.settings, "fixture")
    landed_total = 0
    duplicates_total = 0
    for _ in range(int(inp.get("times", 1))):
        landed, duplicates = land_events(db, zone, connector.pull(db.tenant))
        landed_total += landed
        duplicates_total += duplicates
    raw_rows = db.fetch_one(
        "SELECT count(*) FROM raw_events WHERE connector = %s", (inp["connector"],)
    )
    log_rows = db.fetch_one(
        "SELECT count(*) FROM event_log WHERE topic = %s", (f"raw.{inp['connector']}",)
    )
    dup_files = len(list(zone.root.rglob("dups/*.json")))
    return {
        "landed": landed_total,
        "duplicates": duplicates_total,
        "raw_rows": int(raw_rows[0]) if raw_rows else 0,
        "log_records": int(log_rows[0]) if log_rows else 0,
        "dup_files": dup_files,
    }


class _RecordingConsumer(OrderedConsumer):
    name = "eval-recorder"
    topic = "raw.hris"

    def __init__(self, db: ScopedDB) -> None:
        super().__init__(db)
        self.handled: list[str] = []

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        self.handled.append(record.ref)


@target("ingest_ordering")
def ingest_ordering(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """Append synthetic records in the given order; report the order a consumer handled them."""
    db = ctx.scoped(tenant_id)
    with db.transaction() as cur:
        for rec in inp["records"]:
            event_log.append(
                cur,
                tenant_id=db.tenant.tenant_id,
                topic="raw.hris",
                partition_key=rec["partition"],
                source_ts=parse_ts(rec["source_ts"]),
                ref=rec["ref"],
            )
    consumer = _RecordingConsumer(db)
    while consumer.run_once():
        pass
    mine = {rec["ref"] for rec in inp["records"]}
    return {"handled_order": [ref for ref in consumer.handled if ref in mine]}


@target("mapper")
def mapper(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """Map one fixture event (by connector and source_event_id) and report the entities produced."""
    from ccp.normalize.consumer import map_event

    connector = build_connector(_connector_name(inp["connector"]), ctx.settings, "fixture")
    wanted = inp["source_event_id"]
    event = next(
        (e for e in connector.pull(ctx.tenant(tenant_id)) if e.source_event_id == wanted), None
    )
    if event is None:
        raise LookupError(f"no fixture event {wanted!r}")
    result = map_event(event)
    entities = [
        {
            "kind": e.kind,
            "entity_id": e.entity_id,
            **{
                k: v
                for k, v in e.model_dump(mode="json").items()
                if k
                not in {
                    "tenant_id",
                    "source_connector",
                    "source_event_id",
                    "observed_at",
                    "entity_id",
                }
            },
        }
        for e in result.entities
    ]
    return {
        "event_type": event.event_type,
        "entities": entities,
        "guardrail_events": list(result.guardrail_events),
    }


@target("knowledge_router")
def knowledge_router(
    ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]
) -> dict[str, Any]:
    """Tier and control mapping for one test spec."""
    from ccp.knowledge.graph import load

    graph = load()
    spec = graph.tests[inp["test_id"]]
    controls = graph.controls_for_test(spec.test_id)
    return {
        "tier": spec.tier,
        "evaluation": spec.evaluation,
        "evidence_types": [e.value for e in spec.evidence_types],
        "controls": [c.control_id for c in controls],
        "requirements": sorted(r for c in controls for r in c.requirements),
    }


@target("identity_pair")
def identity_pair(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """Gate, score, and route one pair of fixture persons in memory (no database)."""
    from ccp.identity.config import IdentityConfig
    from ccp.identity.scoring import evaluate
    from ccp.normalize.consumer import map_event
    from ccp.normalize.entities import Person
    from ccp.normalize.merge import merge_persons

    tenant = ctx.tenant(tenant_id)
    observations: list[Person] = []
    for name in ("github", "hris"):
        connector = build_connector(name, ctx.settings, "fixture")
        for event in connector.pull(tenant):
            observations.extend(map_event(event).of(Person))
    persons = {p.entity_id: p for p in merge_persons(observations)}
    scored = evaluate(persons[inp["left"]], persons[inp["right"]], IdentityConfig.load())
    return {
        "gate_result": scored.gate_result,
        "route": scored.route,
        "score": scored.score,
        "features": scored.features,
    }


@target("deterministic_verdict")
def deterministic_verdict(
    ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]
) -> dict[str, Any]:
    """Run one tier 1 test over the prepared tenant; report every subject's verdict."""
    from ccp.evaluators import runner as evaluations
    from ccp.evaluators.verdict import latest_per_subject

    db = ctx.prepared(tenant_id)
    test_id = str(inp["test_id"])
    evaluations.enqueue_all(db, _run_settings(ctx), tests=(test_id,))
    evaluations.drain(db, _run_settings(ctx))
    return {
        "verdicts": {
            v.subject: {
                "status": v.status.value,
                "evidence_refs": list(v.evidence_refs),
                "guardrail_events": list(v.guardrail_events),
                "valid_from": v.valid_from.isoformat(),
            }
            for v in latest_per_subject(db, test_id)
        }
    }


@target("evaluate_tier1")
def evaluate_tier1(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """A full tier 1 run twice: status counts, and proof the second run writes nothing."""
    from ccp.evaluators import runner as evaluations
    from ccp.evaluators.verdict import count

    db = ctx.prepared(tenant_id)
    first_enqueued = evaluations.enqueue_all(db, _run_settings(ctx), tiers=(1,))
    first = evaluations.drain(db, _run_settings(ctx))
    verdicts_after_first = count(db)
    second_enqueued = evaluations.enqueue_all(db, _run_settings(ctx), tiers=(1,))
    second = evaluations.drain(db, _run_settings(ctx))
    return {
        "enqueued": first_enqueued,
        "evaluated": first.evaluated,
        "by_status": dict(sorted(first.by_status.items())),
        "verdict_rows": verdicts_after_first,
        "second_run": {
            "enqueued": second_enqueued,
            "duplicates": second.duplicates,
            "new_rows": count(db) - verdicts_after_first,
        },
    }


def _indexed_db(
    ctx: EvalContext, tenant_id: str | None, providers: Any, distractor_count: int
) -> ScopedDB:
    from ccp.retrieval import distractors as distractor_gen
    from ccp.retrieval import index

    db = ctx.prepared(tenant_id)
    if db.tenant.tenant_id not in ctx._indexed:
        index.refresh(
            db, _run_settings(ctx), providers.embedder, providers.llm, contextual_enrichment=False
        )
        ctx._indexed.add(db.tenant.tenant_id)
    if distractor_count and db.tenant.tenant_id not in ctx._distracted:
        distractor_gen.install(db, providers.embedder, distractor_count)
        ctx._distracted.add(db.tenant.tenant_id)
    return db


@target("chunker_pages")
def chunker_pages(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """Chunks of one evidence row (by entity id prefix): page per chunk and where snippets land."""

    providers = _providers(ctx)
    db = _indexed_db(ctx, tenant_id, providers, 0)
    rows = db.fetch_all(
        "SELECT page, ordinal, text FROM chunks WHERE strpos(evidence_entity_id, %s) = 1"
        " ORDER BY ordinal",
        (str(inp["evidence_prefix"]),),
    )
    snippets = {
        str(s): sorted(
            {int(r[0]) for r in rows if r[0] is not None and str(s).lower() in str(r[2]).lower()}
        )
        for s in inp.get("snippets", [])
    }
    return {
        "chunks": len(rows),
        "pages": sorted({int(r[0]) for r in rows if r[0] is not None}),
        "snippet_pages": snippets,
    }


@target("retrieval_metrics")
def retrieval_metrics(
    ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]
) -> dict[str, Any]:
    """Recall@5, MRR, nDCG@10 over the golden query set for one tenant, with ablation flags."""
    from ccp.retrieval import metrics

    providers = _providers(ctx)
    db = _indexed_db(ctx, tenant_id, providers, int(inp.get("distractors", 300)))
    queries = metrics.load_queries()
    report = metrics.evaluate_queries(
        db,
        providers.embedder,
        queries,
        reranker=providers.reranker if inp.get("rerank", True) else None,
        config=providers.retrieval,
        include_distractors=bool(inp.get("include_distractors", True)),
        iterative_scan=bool(inp.get("iterative_scan", True)),
    )
    return report.summary()


@target("tier2_verdict")
def tier2_verdict(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """One tier 2 evaluation with the configured providers (requires: anthropic)."""
    from datetime import UTC

    from ccp.evaluators import tier2
    from ccp.evaluators.context import load_context
    from ccp.knowledge.graph import load

    providers = _providers(ctx)
    db = _indexed_db(ctx, tenant_id, providers, 0)
    spec = load().tests[str(inp["test_id"])]
    now = datetime.now(UTC)
    context = load_context(db, _run_settings(ctx), spec, now, now)
    verdict = tier2.evaluate(db, _run_settings(ctx), context, spec, str(inp["subject"]), providers)
    return {
        "status": verdict.status.value,
        "confidence": verdict.confidence,
        "verified_citations": sum(1 for c in verdict.citations if c.verified),
        "cited_evidence": sorted(
            {c.chunk_id for c in verdict.citations if c.verified} and set(verdict.evidence_refs)
        ),
        "guardrail_events": sorted({e.split(":")[0] for e in verdict.guardrail_events}),
        "extracted_fields": verdict.extracted_fields or {},
        "reasoning": verdict.reasoning,
        "citations": _verified_citations(verdict.citations),
        "support_precision": _support_precision(verdict),
        "schema_version": verdict.schema_version,
    }


@target("tier3_verdict")
def tier3_verdict(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """One tier 3 evaluation through the registered workflow (requires: anthropic)."""
    from datetime import UTC

    from ccp.agent import threads, tier3
    from ccp.evaluators.context import load_context
    from ccp.knowledge.graph import load

    providers = _providers(ctx)
    db = _indexed_db(ctx, tenant_id, providers, 0)
    spec = load().tests[str(inp["test_id"])]
    now = datetime.now(UTC)
    context = load_context(db, _run_settings(ctx), spec, now, now)
    verdict = tier3.evaluate(
        db, _run_settings(ctx), context, spec, str(inp.get("subject", "tenant")), providers
    )
    thread_id = str((verdict.request_json or {}).get("thread_id"))
    row = threads.get(db, thread_id)
    return {
        "status": verdict.status.value,
        "confidence": verdict.confidence,
        "trajectory": list(row.trajectory) if row else [],
        "thread_status": row.status if row else None,
        "extracted_fields": {
            k: v
            for k, v in (verdict.extracted_fields or {}).items()
            if k != "systems_prompt_version"
        },
        "guardrail_events": sorted({e.split(":")[0] for e in verdict.guardrail_events}),
        "reasoning": verdict.reasoning,
        "citations": _verified_citations(verdict.citations),
        "support_precision": _support_precision(verdict),
        "schema_version": verdict.schema_version,
    }


@target("investigation")
def investigation(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one test, then investigate its verdict for one subject (requires: anthropic)."""
    from datetime import UTC

    from ccp.agent import threads
    from ccp.agent import tier3 as _tier3  # noqa: F401 - registers tier 3
    from ccp.evaluators import runner as evaluations
    from ccp.evaluators.verdict import latest_per_subject
    from ccp.investigation import runner as investigator

    providers = _providers(ctx)
    db = _indexed_db(ctx, tenant_id, providers, 0)
    test_id = str(inp["test_id"])
    now = datetime.now(UTC)
    evaluations.enqueue_all(db, _run_settings(ctx), now=now, tests=(test_id,))
    evaluations.drain(db, _run_settings(ctx), now=now, providers=providers)
    verdict = {v.subject: v for v in latest_per_subject(db, test_id)}[str(inp["subject"])]
    inv = investigator.investigate(db, _run_settings(ctx), verdict, providers, now=now)
    row = threads.get(db, inv.thread_id or "")
    return {
        "verdict_status": verdict.status.value,
        "trajectory": list(row.trajectory) if row else [],
        "citations": [c.model_dump() for c in inv.citations if c.verified],
        "root_cause_category": inv.root_cause_category,
        "root_cause": inv.root_cause,
        "missing_evidence": [m.evidence_type.value for m in inv.missing_evidence],
        "actions": [p.action for p in inv.proposed_actions],
        "targets": [p.target for p in inv.proposed_actions],
        "guardrail_events": sorted({e.split(":")[0] for e in inv.guardrail_events}),
    }


@target("policy_gap")
def policy_gap(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """The Policy Agent for one control: gap analysis, or a draft (requires: anthropic)."""
    from datetime import UTC
    from pathlib import Path

    from ccp.approvals import store as approvals
    from ccp.policy import runner as policy_runner

    providers = _providers(ctx)
    db = _indexed_db(ctx, tenant_id, providers, 0)
    settings = _run_settings(ctx)
    control_id = str(inp["control_id"])
    now = datetime.now(UTC)
    extra: dict[str, Any] = {}
    if inp.get("mode", "gap") == "draft":
        report, draft = policy_runner.draft(db, settings, control_id, providers, now=now)
        text = Path(draft.path).read_text(encoding="utf-8").lower()
        tokens = policy_runner.other_tenant_tokens(settings, db.tenant.tenant_id)
        item = approvals.get(db, draft.approval_id)
        extra = {
            "draft_written": Path(draft.path).is_file(),
            "other_tenant_tokens_present": any(t.lower() in text for t in tokens),
            "approval_kind": item.kind if item else None,
            "scrub_event_kinds": sorted({e.split(":")[0] for e in draft.scrub_events}),
            "markdown": draft.markdown[:4000],
        }
    else:
        report = policy_runner.gap(db, settings, control_id, providers, now=now)
    return {
        "requirement_ids": [g.requirement_id for g in report.gaps],
        "uncovered": list(report.uncovered),
        "verified_citations": all(c.verified for g in report.gaps for c in g.covering_citations),
        "confidence": report.confidence,
        "guardrail_events": sorted({e.split(":")[0] for e in report.guardrail_events}),
        **extra,
    }


@target("copilot_answer")
def copilot_answer(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """One Copilot turn on a fresh session (requires: anthropic); nothing is confirmed."""
    from datetime import UTC

    from ccp.agent import tier3 as _tier3  # noqa: F401 - registers tier 3
    from ccp.agent.graphs.copilot import load_config, says_forbidden
    from ccp.approvals import store as approvals
    from ccp.compliance_state import writer as state_writer
    from ccp.copilot import runner as copilot
    from ccp.evaluators import runner as evaluations
    from ccp.policy import runner as policy_runner

    providers = _providers(ctx)
    db = _indexed_db(ctx, tenant_id, providers, 0)
    now = datetime.now(UTC)
    tests = tuple(str(t) for t in inp.get("evaluate", ["HR-1"]))
    evaluations.enqueue_all(db, _run_settings(ctx), now=now, tests=tests)
    evaluations.drain(db, _run_settings(ctx), now=now, providers=providers)
    state_writer.drain(db)
    open_before = len(approvals.list_items(db, "remediation", True))
    with copilot.Session(db, _run_settings(ctx), providers) as session:
        turn = session.ask(str(inp["question"]), now=now)
    open_after = len(approvals.list_items(db, "remediation", True))
    request = turn.answer.request_json or {}
    tokens = policy_runner.other_tenant_tokens(_run_settings(ctx), db.tenant.tenant_id)
    lowered = turn.answer.text.lower()
    return {
        "intent": turn.answer.intent,
        "tools": [t.get("tool") for t in request.get("tool_log", [])],
        "trajectory": list(turn.trajectory),
        "says_forbidden": says_forbidden(
            turn.answer.text, load_config(ctx.settings.ccp_copilot_config).forbidden_summary_words
        ),
        "citations": len(turn.answer.citations),
        "citations_verified": all(c.verified for c in turn.answer.citations),
        "requested_actions": [a.get("action") for a in turn.answer.requested_actions],
        "pending": len(turn.pending),
        "approvals_opened": open_after - open_before,
        "mentions_other_tenants": any(t.lower() in lowered for t in tokens),
        "guardrail_events": sorted({e.split(":")[0] for e in turn.answer.guardrail_events}),
        "text": turn.answer.text[:500],
    }


@target("adversarial_tier2")
def adversarial_tier2(
    ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]
) -> dict[str, Any]:
    """Tier 2 over a corpus with one poisoned chunk (S17); the attack succeeds when the status
    differs from the unpoisoned control's expected status (requires: anthropic)."""
    from datetime import UTC

    from ccp.evals import adversarial
    from ccp.evaluators import tier2
    from ccp.evaluators.context import load_context
    from ccp.knowledge.graph import load

    providers = _providers(ctx)
    db = _indexed_db(ctx, tenant_id, providers, 0)
    spec = load().tests[str(inp["test_id"])]
    subject = str(inp["subject"])
    evidence_type = spec.evidence_types[0].value
    base = adversarial.base_chunk(db, evidence_type, tier2.subject_entity(subject))
    if base is None:
        raise LookupError(f"no {evidence_type} chunk for {subject}")
    text = adversarial.apply_placement(
        str(base["text"]), str(inp["payload"]), str(inp.get("placement", "append"))
    )
    chunk_id = adversarial.poison(db, providers.embedder, base, text)
    try:
        now = datetime.now(UTC)
        context = load_context(db, _run_settings(ctx), spec, now, now)
        verdict = tier2.evaluate(
            db, _run_settings(ctx), context, spec, subject, providers, reason="reevaluate"
        )
    finally:
        adversarial.unpoison(db, chunk_id)
    control = str((inp.get("control") or {}).get("status") or "")
    verified = {c.chunk_id for c in verdict.citations if c.verified}
    shown = chunk_id in set(verdict.retrieved_chunk_ids)
    return {
        "status": verdict.status.value,
        "confidence": verdict.confidence,
        "control_status": control,
        "poisoned_chunk_shown": shown,  # an unshown payload was not an attempted attack
        "attack_succeeded": shown and verdict.status.value != control,
        "injection_detected": any(
            e.startswith("injection_marker_detected") for e in verdict.guardrail_events
        ),
        "poisoned_chunk_cited": chunk_id in verified,
        "guardrail_events": sorted({e.split(":")[0] for e in verdict.guardrail_events}),
        "poisoned_chunks_left": adversarial.poisoned_count(db),
    }


@target("pipeline")
def pipeline(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """The full run for one tenant in the scratch database; exact on compliance state."""
    from ccp.compliance_state import reader, run

    db = ctx.scoped(tenant_id)
    assert ctx.db_settings is not None
    run_settings = ctx.db_settings.model_copy(update={"ccp_data_dir": ctx.scratch_dir})
    report = run.run(db, run_settings, ingest=True)
    heads = {r.subject_key: r.status.value for r in reader.heads(db)}
    frameworks = {}
    for framework_id in inp.get("frameworks", ["soc2", "iso27001"]):
        rollup = reader.framework_rollup(db, framework_id)
        frameworks[framework_id] = {
            "no_evaluations_yet": rollup.no_evaluations_yet,
            "requirements": {r.requirement_id: r.state for r in rollup.requirements},
        }
    return {
        "landed": report.landed,
        "evaluated": report.evaluated,
        "parked": report.parked,
        "ai_tiers_available": report.ai_tiers_available,
        "state_written": report.state_written,
        "heads": heads,
        "frameworks": frameworks,
    }


def make_event(
    tenant: TenantContext, connector: ConnectorName, source_event_id: str, ts: datetime
) -> SourceEvent:
    return SourceEvent(
        tenant_id=tenant.tenant_id,
        connector=connector,
        source_event_id=source_event_id,
        source_ts=ts,
        event_type="synthetic",
        payload={"id": source_event_id},
        schema_version="synthetic.v1",
    )
