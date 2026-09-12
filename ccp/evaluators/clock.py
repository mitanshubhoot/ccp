"""The clock producer (spec.md D11): time passing is not an entity change, so a scheduled
producer enqueues subjects whose latest verdict is older than the test's revalidate_every,
or whose newest cited evidence is older than its evidence_max_age. The verdict that
results carries the reason, and the state writer records it as `stale_evidence`.

    ccp worker run --consumer clock --once
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ccp.evaluators.impact import EvaluationRequest, enqueue
from ccp.evaluators.verdict import latest_per_subject
from ccp.ingest.worker import register
from ccp.knowledge import graph as knowledge
from ccp.normalize import repository
from ccp.observability import steps
from ccp.storage.scoped import ScopedDB


@dataclass
class ClockSummary:
    enqueued: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def add(self, reason: str) -> None:
        self.enqueued += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


def tick(db: ScopedDB, now: datetime | None = None) -> ClockSummary:
    """One scheduler pass for one tenant. `now` is injectable so the stale case is testable."""
    graph = knowledge.load()
    moment = now or datetime.now(UTC)
    evidence_observed = {e.entity_id: e.observed_at for e in repository.current_evidence(db)}
    summary = ClockSummary()
    with steps.step(
        "clock.tick", layer="evaluators", tenant=db.tenant, inputs={"now": moment.isoformat()}
    ) as s:
        for spec in graph.tests.values():
            interval = spec.revalidate_every
            max_age = spec.evidence_max_age
            if interval is None and max_age is None:
                continue
            for verdict in latest_per_subject(db, spec.test_id):
                reason: str | None = None
                if interval is not None and verdict.created_at + interval <= moment:
                    reason = "revalidate_interval"
                if max_age is not None:
                    cited = [
                        evidence_observed[r]
                        for r in verdict.evidence_refs
                        if r in evidence_observed
                    ]
                    if cited and max(cited) + max_age <= moment:
                        reason = "stale_evidence"
                if reason is None:
                    continue
                request = EvaluationRequest(spec.test_id, verdict.subject, moment, reason=reason)
                with db.transaction() as cur:
                    if enqueue(cur, db.tenant.tenant_id, request):
                        summary.add(reason)
        s.set_output({"enqueued": summary.enqueued, "reasons": summary.reasons})
    return summary


class ClockProducer:
    """Worker-loop adapter: one tick per run_once; returns how many records it enqueued."""

    name = "clock"
    topic = "evaluations"

    def __init__(self, db: ScopedDB, now: datetime | None = None) -> None:
        self._db = db
        self.now = now

    def run_once(self) -> int:
        return tick(self._db, self.now).enqueued


register("clock")(lambda db, settings: ClockProducer(db))
