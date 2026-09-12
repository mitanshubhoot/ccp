"""The evaluations consumer and the full-run enqueuer (spec.md Layer 7).

    evaluations record ──► budget gate ──► route by tier ──► evaluate ──► persist (idempotent)
                                                                         └► verdicts topic
Tier 1 runs here. Tiers 2 and 3 are parked until their evaluators arrive (Phases 7b, 8);
parked records are re-queued with `requeue_parked`. The tenant's `max_concurrency` caps how
many records one pass claims, so a burst never runs more evaluations at once than allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache

from ccp.config import Settings
from ccp.evaluators import deterministic
from ccp.evaluators.budget_gate import check_daily_budget
from ccp.evaluators.context import load_context
from ccp.evaluators.impact import EvaluationRequest, enqueue
from ccp.evaluators.verdict import TriggerReason, Verdict, persist
from ccp.ingest import event_log
from ccp.ingest.consumers import ParallelConsumer, ParkedError
from ccp.ingest.event_log import LogRecord
from ccp.ingest.worker import register
from ccp.knowledge import graph as knowledge
from ccp.knowledge.graph import KnowledgeGraph
from ccp.normalize import repository
from ccp.observability import steps
from ccp.storage.scoped import ScopedDB

_REASONS: tuple[TriggerReason, ...] = (
    "impact",
    "full_run",
    "revalidate_interval",
    "stale_evidence",
    "reevaluate",
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
    db: ScopedDB, settings: Settings, request: EvaluationRequest, now: datetime | None = None
) -> Verdict:
    """Evaluate one (test, subject) pair by tier. Tier 1 only in Phase 7a."""
    spec = cached_graph().tests[request.test_id]
    if spec.tier != 1:
        raise ParkedError(f"tier {spec.tier} evaluator not available yet for {spec.test_id}")
    ctx = load_context(
        db, settings, spec, request.trigger_observed_at, now, reason=_reason(request.reason)
    )
    return deterministic.evaluate(ctx, request.subject)


def commit_verdict(db: ScopedDB, verdict: Verdict) -> bool:
    """Persist and announce. Returns False when the evaluation key already existed."""
    inserted = persist(db, verdict)
    if inserted:
        with db.transaction() as cur:
            event_log.append(
                cur,
                tenant_id=verdict.tenant_id,
                topic="verdicts",
                partition_key=f"{verdict.tenant_id}:{verdict.subject_key}",
                source_ts=verdict.valid_from,
                ref=verdict.verdict_id,
            )
    return inserted


class EvaluationsConsumer(ParallelConsumer):
    name = "evaluations"
    topic = "evaluations"

    def __init__(self, db: ScopedDB, settings: Settings, now: datetime | None = None) -> None:
        super().__init__(
            db,
            max_attempts=settings.ccp_max_attempts,
            visibility_seconds=settings.ccp_claim_visibility_seconds,
        )
        self.settings = settings
        self.now = now
        self.summary = RunSummary()

    def in_flight(self) -> int:
        row = self._db.fetch_one(
            "SELECT count(*) FROM event_log WHERE topic = %s AND status = 'claimed'"
            " AND claimed_at >= now() - make_interval(secs => %s)",
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
        check_daily_budget(self._db, self.now)
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
            verdict = evaluate_request(self._db, self.settings, request, self.now)
            inserted = commit_verdict(self._db, verdict)
            s.set_output(
                {
                    "verdict_id": verdict.verdict_id,
                    "status": verdict.status.value,
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
) -> int:
    """Enqueue every (test, subject) pair once, with the newest entity observation as the
    trigger so a repeated full run is idempotent at the verdict table."""
    trigger = repository.latest_observed_at(db) or datetime.now(UTC)
    count = 0
    for spec in cached_graph().tests.values():
        if tests is not None and spec.test_id not in tests:
            continue
        if spec.tier != 1:
            continue  # tiers 2 and 3 arrive in Phases 7b and 8
        ctx = load_context(db, settings, spec, trigger, now, reason="full_run")
        with db.transaction() as cur:
            for subject in deterministic.subjects_for(ctx):
                request = EvaluationRequest(spec.test_id, subject, trigger, reason="full_run")
                if enqueue(cur, db.tenant.tenant_id, request, coalesce_seconds):
                    count += 1
    return count


def drain(db: ScopedDB, settings: Settings, now: datetime | None = None) -> RunSummary:
    consumer = EvaluationsConsumer(db, settings, now)
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
