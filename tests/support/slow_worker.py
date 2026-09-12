"""A parallel consumer that can be killed mid-handle; used by the two-worker test.

    python slow_worker.py <database_url> <tenant> <marker_dir> <sleep_seconds> <visibility_seconds>

Claims one record from the `evaluations` topic, writes `<marker_dir>/started-<pid>`, sleeps,
then writes one line to `<marker_dir>/effects` and marks the record done.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from ccp.config import Settings
from ccp.ingest.consumers import ParallelConsumer
from ccp.ingest.event_log import LogRecord
from ccp.storage.db import app_pool
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext


class SlowConsumer(ParallelConsumer):
    name = "slow"
    topic = "evaluations"

    def __init__(
        self, db: ScopedDB, marker_dir: Path, sleep_seconds: float, visibility: int
    ) -> None:
        super().__init__(db, max_attempts=5, visibility_seconds=visibility)
        self.marker_dir = marker_dir
        self.sleep_seconds = sleep_seconds

    def handle(self, record: LogRecord) -> None:
        import os

        (self.marker_dir / f"started-{os.getpid()}").write_text(
            str(record.offset), encoding="utf-8"
        )
        time.sleep(self.sleep_seconds)
        with (self.marker_dir / "effects").open("a", encoding="utf-8") as fh:
            fh.write(f"done:{record.offset}:{os.getpid()}\n")


def main() -> None:
    database_url, tenant_id, marker_dir, sleep_seconds, visibility = sys.argv[1:6]
    settings = Settings(database_url=database_url)
    pool = app_pool(settings, max_size=2)
    try:
        db = ScopedDB(pool, TenantContext.for_id(settings, tenant_id))
        consumer = SlowConsumer(db, Path(marker_dir), float(sleep_seconds), int(visibility))
        consumer.run_once(limit=1)
    finally:
        pool.close()


if __name__ == "__main__":
    main()
