"""The evaluations consumer and the full-run enqueuer (spec.md Layer 7, A-97).

    evaluations record ──► route by tier ──► stored verdict? ──► budget gate (AI tiers) ──►
        evaluate ──► one transaction: verdict (idempotent) + its announcement on `verdicts`
Tier 1 never parks: the daily model budget applies only to tiers 2 and 3. A record whose
verdict is already stored makes no model call. A tier 2 record with no model client
configured parks (AI tier unavailable), never dead-letters. The tenant's `max_concurrency`
caps how many records one pass claims.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache

from ccp.config import Settings, TenantSettings
from ccp.evaluators import deterministic, registry, tier2
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.budget_gate import check_daily_budget
from ccp.evaluators.context import load_context
from ccp.evaluators.impact import EvaluationRequest, enqueue
from ccp.evaluators.rule_versions import rule_stamp
from ccp.evaluators.verdict import TriggerReason, Verdict, evaluation_key
from ccp.ingest import event_log
from ccp.ingest.consumers import ParallelConsumer, ParkedError
from ccp.ingest.event_log import LogRecord
from ccp.ingest.worker import register
from ccp.knowledge import graph as knowledge
from ccp.knowledge.graph import KnowledgeGraph
from ccp.normalize import repository
from ccp.observability import steps
from ccp.retrieval.hybrid import EmbeddingModelMismatch
from ccp.storage.scoped import ScopedDB

_REASONS: tuple[TriggerReason, ...] = (
    "impact",
    "full_run",
    "revalidate_interval",
    "stale_evidence",
    "reevaluate",
    "human_decision",
)


@lru_cache(maxsize=1)
def cached_graph() -> KnowledgeGraph:
    return knowledge.load()


def _reason(value: str) -> TriggerReason:
    for known in _REASONS:
        if value == known:
            return known
    return "impact"


@dataclass
class RunSummary:
    enqueued: int = 0
    evaluated: int = 0
    duplicates: int = 0
    by_status: dict[str, int] = field(default_factory=dict)


def evaluate_request(
    db: ScopedDB,
    settings: Settings,
    request: EvaluationRequest,
    now: datetime | None = None,
    providers: tier2.Providers | None = None,
) -> Verdict:
    """Evaluate one (test, subject) pair by tier."""
    spec = cached_graph().tests[request.test_id]
    ctx = load_context(
        db, settings, spec, request.trigger_observed_at, now, reason=_reason(request.reason)
    )
    if spec.tier == 1:
        return deterministic.evaluate(ctx, request.subject)
    evaluator = registry.evaluator_for(spec.tier)
    if evaluator is None:
        raise ParkedError(
            f"tier {spec.tier} evaluator not registered (import ccp.agent.tier3 for tier 3)"
        )
    active = providers or tier2.Providers.from_settings(settings)
    try:
        return evaluator(
            db, settings, ctx, spec, request.subject, active, reason=_reason(request.reason)
        )
    except tier2.AITierUnavailable as exc:
        raise ParkedError(str(exc)) from exc
    except EmbeddingModelMismatch as exc:
        raise ParkedError(str(exc)) from exc


def commit_verdict(db: ScopedDB, verdict: Verdict) -> bool:
    """Persist and announce in one transaction, so a crash cannot leave a verdict that the
    state writer never hears of. Returns False when the evaluation key already existed."""
    with db.transaction() as cur:
        inserted = verdicts.insert(cur, verdict)
        if inserted:
            event_log.append(
                cur,
                tenant_id=verdict.tenant_id,
                topic="verdicts",
                partition_key=f"{verdict.tenant_id}:{verdict.subject_key}",
                source_ts=verdict.valid_from,
                ref=verdict.verdict_id,
            )
    return inserted


def cached_tenant_settings(tenant_id: str, settings: Settings) -> TenantSettings:
    return settings.load_tenants()[tenant_id]


def expected_key(settings: Settings, tenant_id: str, request: EvaluationRequest) -> str:
    """The evaluation key the routed tier will use, known before it runs."""
    spec = cached_graph().tests[request.test_id]
    model = {2: settings.ccp_tier2_model, 3: settings.ccp_tier3_model}.get(spec.tier)
    return evaluation_key(
        tenant_id,
        spec.test_id,
        f"{spec.test_id}|{request.subject}",
        request.trigger_observed_at,
        spec.prompt_version if spec.tier in (2, 3) else None,
        model,
        rule=rule_stamp(spec, cached_tenant_settings(tenant_id, settings)),
    )


def unannounced(db: ScopedDB) -> int:
    """Stored verdicts with no announcement on `verdicts`: zero, now that both commit
    together (the NT4 reconciler check)."""
    row = db.fetch_one(
        "SELECT count(*) FROM verdicts v WHERE NOT EXISTS (SELECT 1 FROM event_log e"
        " WHERE e.topic = 'verdicts' AND e.ref = v.verdict_id)"
    )
    return int(row[0]) if row else 0


class EvaluationsConsumer(ParallelConsumer):
    name = "evaluations"
    topic = "evaluations"

    def __init__(
        self,
        db: ScopedDB,
        settings: Settings,
        now: datetime | None = None,
        providers: tier2.Providers | None = None,
    ) -> None:
        super().__init__(
            db,
            max_attempts=settings.ccp_max_attempts,
            visibility_seconds=settings.ccp_claim_visibility_seconds,
            base_delay=settings.ccp_retry_base_seconds,
        )
        self.settings = settings
        self.now = now
        self._providers = providers
        self.summary = RunSummary()

    @property
    def providers(self) -> tier2.Providers:
        if self._providers is None:
            self._providers = tier2.Providers.from_settings(self.settings)
        return self._providers

    def in_flight(self) -> int:
        """Records held under a live lease (A-105); a claim from before leases counts until
        its old visibility timeout."""
        row = self._db.fetch_one(
            "SELECT count(*) FROM event_log WHERE topic = %s AND status = 'claimed'"
            " AND coalesce(lease_expires_at, claimed_at + make_interval(secs => %s)) > now()",
            (self.topic, self.visibility_seconds),
        )
        return int(row[0]) if row else 0

    def run_once(self, limit: int = 10) -> int:
        """Claim at most (max_concurrency - in flight) records, never more than `limit`."""
        allowed = min(limit, self._db.tenant.settings.max_concurrency - self.in_flight())
        if allowed <= 0:
            return 0
        return super().run_once(allowed)

    def handle(self, record: LogRecord) -> None:
        request = EvaluationRequest.from_ref(record.ref)
        spec = cached_graph().tests[request.test_id]
        with steps.step(
            "evaluate",
            layer="evaluators",
            tenant=self._db.tenant,
            inputs={
                "test_id": request.test_id,
                "subject": request.subject,
                "offset": record.offset,
                "reason": request.reason,
            },
        ) as s:
            stored = verdicts.load_by_key(
                self._db, expected_key(self.settings, self._db.tenant.tenant_id, request)
            )
            if stored is not None:  # done before (a retry after a crash): no model call
                verdict, inserted = stored, False
                s.event("verdict_already_stored")
            else:
                if spec.tier in (2, 3):  # only model calls spend the daily budget
                    check_daily_budget(self._db, self.now)
                providers = self.providers if spec.tier in (2, 3) else None
                verdict = evaluate_request(self._db, self.settings, request, self.now, providers)
                inserted = commit_verdict(self._db, verdict)
            s.set_output(
                {
                    "verdict_id": verdict.verdict_id,
                    "status": verdict.status.value,
                    "confidence": verdict.confidence,
                    "inserted": inserted,
                }
            )
            for event in verdict.guardrail_events:
                s.event(event)
        self.summary.evaluated += 1
        if not inserted:
            self.summary.duplicates += 1
        self.summary.by_status[verdict.status.value] = (
            self.summary.by_status.get(verdict.status.value, 0) + 1
        )


register("evaluations")(lambda db, settings: EvaluationsConsumer(db, settings))


def enqueue_all(
    db: ScopedDB,
    settings: Settings,
    now: datetime | None = None,
    coalesce_seconds: int = 0,
    tests: tuple[str, ...] | None = None,
    tiers: tuple[int, ...] = (1, 2, 3),
) -> int:
    """Enqueue every (test, subject) pair once, with the newest entity observation as the
    trigger so a repeated full run is idempotent at the verdict table."""
    trigger = repository.latest_observed_at(db) or datetime.now(UTC)
    count = 0
    for spec in cached_graph().tests.values():
        if tests is not None and spec.test_id not in tests:
            continue
        if spec.tier not in tiers:
            continue
        ctx = load_context(db, settings, spec, trigger, now, reason="full_run")
        subjects = (
            deterministic.subjects_for(ctx) if spec.tier == 1 else tier2.subjects_for(ctx, spec)
        )
        with db.transaction() as cur:
            for subject in subjects:
                request = EvaluationRequest(spec.test_id, subject, trigger, reason="full_run")
                if enqueue(cur, db.tenant.tenant_id, request, coalesce_seconds):
                    count += 1
    return count


def drain(
    db: ScopedDB,
    settings: Settings,
    now: datetime | None = None,
    providers: tier2.Providers | None = None,
) -> RunSummary:
    consumer = EvaluationsConsumer(db, settings, now, providers)
    while consumer.run_once(limit=50):
        pass
    return consumer.summary


def requeue_parked(db: ScopedDB, topic: str = "evaluations") -> int:
    return db.execute(
        "UPDATE event_log SET status = 'pending', last_error = NULL"
        " WHERE topic = %s AND status = 'parked'",
        (topic,),
    )


def parked_count(db: ScopedDB, topic: str = "evaluations") -> int:
    row = db.fetch_one(
        "SELECT count(*) FROM event_log WHERE topic = %s AND status = 'parked'", (topic,)
    )
    return int(row[0]) if row else 0
