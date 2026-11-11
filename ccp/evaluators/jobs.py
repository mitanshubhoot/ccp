"""Scheduled jobs (spec.md A-108): the scheduler appends one record per fired slot to the
`jobs` topic, and this consumer runs it, one at a time per schedule.

    clock      the staleness check: enqueue subjects whose verdict or evidence is too old
    unpark     parked evaluations and investigations back to pending (the model budget's
               day has rolled over)
    full_run   every (test, subject) pair, idempotent at the verdict table
    check_connections   every enabled connection's credential probed, each check recorded
    sync       every enabled connection pulled, each pull its own sync run (A-121)
    replay_sentinel   a sample of tier-1 verdicts re-derived from their manifests (A-125)
    audit_checkpoint  the audit chain's head signed and exported (A-137)
    audit_verify      the chain walked, its rows re-hashed, its checkpoints checked; any
                      finding fails the job, so it shows (A-137)

Every job is safe to run twice: the clock and the full run enqueue through the evaluation
key, unparking an unparked topic moves nothing, a second check is another record, and a
second pull of unchanged data lands no snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ccp.audit import checkpoints as audit_checkpoints
from ccp.audit import verify as audit_verify
from ccp.config import Settings
from ccp.evaluators import clock, rederive
from ccp.evaluators import runner as evaluations
from ccp.ingest import connections, ops
from ccp.ingest.consumers import ParallelConsumer, TerminalError
from ccp.ingest.event_log import LogRecord
from ccp.ingest.landing import sync_connections
from ccp.ingest.schedules import JOBS_TOPIC
from ccp.ingest.worker import register
from ccp.observability import steps
from ccp.storage.scoped import ScopedDB

Job = Callable[[ScopedDB, Settings, datetime], int]
UNPARKED = ("evaluations", "investigations")  # the topics that park for the model budget
JOBS: dict[str, Job] = {
    "clock": lambda db, settings, slot: clock.tick(db).enqueued,
    "unpark": lambda db, settings, slot: sum(ops.unpark(db, t) for t in UNPARKED),
    "full_run": lambda db, settings, slot: evaluations.enqueue_all(db, settings),
    "check_connections": lambda db, settings, slot: len(connections.check_all(settings, db)),
    "sync": lambda db, settings, slot: len(sync_connections(settings, db, db.tenant).runs),
    "replay_sentinel": lambda db, settings, slot: len(rederive.sentinel(db, settings)),
    "audit_checkpoint": lambda db, settings, slot: int(
        audit_checkpoints.checkpoint(db, settings) is not None
    ),
    "audit_verify": lambda db, settings, slot: _verified(db, settings),
}


def _verified(db: ScopedDB, settings: Settings) -> int:
    report = audit_verify.verify(db, settings)
    if not report.clean:
        raise RuntimeError("audit findings: " + "; ".join(report.findings[:10]))
    return report.entries


class JobsConsumer(ParallelConsumer):
    name = "jobs"
    topic = JOBS_TOPIC
    one_per_key = True  # a schedule's runs never overlap (A-107, A-108)

    def __init__(self, db: ScopedDB, settings: Settings) -> None:
        super().__init__(
            db,
            max_attempts=settings.ccp_max_attempts,
            visibility_seconds=settings.ccp_claim_visibility_seconds,
            base_delay=settings.ccp_retry_base_seconds,
        )
        self.settings = settings
        self.ran: list[tuple[str, str, int]] = []

    def handle(self, record: LogRecord) -> None:
        data: dict[str, Any] = json.loads(record.ref)
        job = JOBS.get(str(data.get("job")))
        if job is None:
            raise TerminalError(f"unknown job {data.get('job')!r}")
        slot = datetime.fromisoformat(str(data["slot"]))
        with steps.step("job.run", layer="evaluators", tenant=self._db.tenant, inputs=data) as s:
            result = job(self._db, self.settings, slot)
            s.set_output({"result": result})
        self.ran.append((str(data["schedule_id"]), str(data["job"]), result))


register("jobs")(lambda db, settings: JobsConsumer(db, settings))
