"""A worker process for the chaos test (A-111): the real worker loop over the real
consumers, with a pause in each evaluation so a kill lands mid-step.

    python chaos_worker.py <database_url> <consumer> <tenant> <pause_ms> <lease_seconds>

Runs until it is killed.
"""

from __future__ import annotations

import sys
import time

from ccp.compliance_state import writer as _writer  # noqa: F401 - registers the state writer
from ccp.config import Settings
from ccp.evaluators import runner
from ccp.ingest import worker
from ccp.ingest.event_log import LogRecord
from ccp.observability import steps
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext


def main() -> None:
    database_url, consumer, tenant_id, pause_ms, lease = sys.argv[1:6]
    settings = Settings(database_url=database_url, ccp_claim_visibility_seconds=float(lease))
    steps.configure(settings, sink=None)
    pause = int(pause_ms) / 1000

    class PausedEvaluations(runner.EvaluationsConsumer):
        def handle(self, record: LogRecord) -> None:
            time.sleep(pause)  # a kill now leaves the record claimed mid-step
            super().handle(record)

    def paused(db: ScopedDB, s: Settings) -> worker.Runnable:
        return PausedEvaluations(db, s)

    worker.register("evaluations")(paused)
    tenants = [TenantContext.for_id(settings, tenant_id)]
    worker.run(settings, consumer, tenants, once=False, poll_seconds=0.5)


if __name__ == "__main__":
    main()
