"""MT2 (A-105): a parallel claim is a lease that a heartbeat keeps while the worker works.

The defects: a claim expired after a fixed visibility timeout, so a handler slower than it
was re-claimed and run twice by a second worker; and every status change after the claim
was unconditional, so the first worker, finishing late, could flip a record the second had
already completed back to pending (or dead-letter it). Now the holder's heartbeat renews the
lease, and every status change presents the claim's token.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from ccp.config import Settings
from ccp.ingest import event_log
from ccp.ingest.consumers import ParallelConsumer
from ccp.ingest.event_log import LogRecord
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

pytestmark = pytest.mark.db
TS = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def lone(base_settings: Settings) -> Iterator[ScopedDB]:
    """A database of its own: a parallel consumer claims every pending record of its topic."""
    try:
        with scratch_database(base_settings, prefix="ccp_lease") as settings:
            pool = app_pool(settings, max_size=6)
            try:
                yield ScopedDB(pool, TenantContext.for_id(settings, "acme"))
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


class _Worker(ParallelConsumer):
    name = "lease-test"
    topic = "evaluations"

    def __init__(self, db: ScopedDB, lease: float, sleep: float, log: list[str]) -> None:
        super().__init__(db, max_attempts=5, visibility_seconds=lease, base_delay=0.0)
        self.sleep = sleep
        self.log = log

    def handle(self, record: LogRecord) -> None:
        self.log.append(f"start:{record.ref}")
        time.sleep(self.sleep)
        self.log.append(f"end:{record.ref}")


def _append(db: ScopedDB, ref: str) -> int:
    with db.transaction() as cur:
        return event_log.append(
            cur, tenant_id="acme", topic="evaluations", partition_key=ref, source_ts=TS, ref=ref
        )


def _status(db: ScopedDB, offset: int) -> tuple[str, int, str | None]:
    with db.transaction() as cur:
        return event_log.status_of(cur, offset)


def _wait_for(predicate: Any, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_a_handler_slower_than_its_lease_keeps_it_and_runs_once(lone: ScopedDB) -> None:
    offset = _append(lone, "r1")
    log: list[str] = []
    slow = _Worker(lone, lease=1.0, sleep=2.5, log=log)
    rival = _Worker(lone, lease=1.0, sleep=0.0, log=log)
    runner = threading.Thread(target=slow.run_once)
    runner.start()
    _wait_for(lambda: "start:r1" in log, 10)
    taken = 0
    while runner.is_alive():  # well past the 1 s lease: the heartbeat keeps it
        taken += rival.run_once()
        time.sleep(0.1)
    runner.join()
    assert taken == 0
    assert log == ["start:r1", "end:r1"]
    assert _status(lone, offset) == ("done", 1, None)
    assert slow.lost_leases == 0


def test_a_worker_that_lost_its_lease_cannot_move_the_record(lone: ScopedDB) -> None:
    offset = _append(lone, "r2")
    with lone.transaction() as cur:
        (stale,) = event_log.claim(cur, "evaluations", 1, 0.2, "paused-worker")
    time.sleep(0.4)  # its lease runs out with no heartbeat (a paused process)
    log: list[str] = []
    assert _Worker(lone, lease=5.0, sleep=0.0, log=log).run_once() == 1
    assert _status(lone, offset) == ("done", 2, None)
    with lone.transaction() as cur:  # the paused worker wakes and reports its outcome
        assert not event_log.mark_retry(cur, offset, "late failure", 0.0, lease=stale.lease_token)
        assert not event_log.mark_dead(cur, stale, "late failure", lease=stale.lease_token)
        assert not event_log.mark_done(cur, offset, lease=stale.lease_token)
        assert not event_log.mark_parked(cur, offset, "late", lease=stale.lease_token)
        dead = cur.execute('SELECT count(*) FROM dead_letter WHERE "offset" = %s', (offset,))
        assert dead.fetchone() == (0,)
    assert _status(lone, offset) == ("done", 2, None)


def test_a_consumer_whose_heartbeat_fails_drops_its_outcome(
    lone: ScopedDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heartbeat cannot reach the record (as if partitioned from the database): a rival
    re-claims it after the lease and finishes it; the first worker's late outcome is dropped."""
    offset = _append(lone, "r3")
    log: list[str] = []
    stuck = _Worker(lone, lease=0.5, sleep=1.5, log=log)
    monkeypatch.setattr(event_log, "renew", lambda cur, held, seconds: set())
    runner = threading.Thread(target=stuck.run_once)
    runner.start()
    _wait_for(lambda: "start:r3" in log, 10)
    time.sleep(0.7)  # past the lease
    rival = _Worker(lone, lease=5.0, sleep=0.0, log=log)
    assert rival.run_once() == 1
    runner.join()
    assert _status(lone, offset) == ("done", 2, None)
    assert stuck.lost_leases == 1
    assert log.count("start:r3") == 2  # the lost lease cannot stop a run in progress


def test_a_claim_from_before_leases_keeps_its_old_timeout(lone: ScopedDB) -> None:
    offset = _append(lone, "r4")
    with lone.transaction() as cur:  # claimed by a worker that predates leases
        cur.execute(
            "UPDATE event_log SET status = 'claimed', attempts = 1,"
            " claimed_at = now() - interval '10 seconds' WHERE \"offset\" = %s",
            (offset,),
        )
        assert event_log.claim(cur, "evaluations", 5, 60.0) == []
        cur.execute(
            "UPDATE event_log SET claimed_at = now() - interval '120 seconds'"
            ' WHERE "offset" = %s',
            (offset,),
        )
        (taken,) = event_log.claim(cur, "evaluations", 5, 60.0)
    assert taken.offset == offset and taken.attempts == 2 and taken.lease_token


def test_records_waiting_in_a_claimed_batch_keep_their_leases(lone: ScopedDB) -> None:
    first, second = _append(lone, "b1"), _append(lone, "b2")
    log: list[str] = []
    batch = _Worker(lone, lease=0.6, sleep=1.5, log=log)
    rival = _Worker(lone, lease=0.6, sleep=0.0, log=log)
    runner = threading.Thread(target=lambda: batch.run_once(limit=2))
    runner.start()
    _wait_for(lambda: "start:b1" in log, 10)
    taken = 0
    while runner.is_alive():
        taken += rival.run_once()
        time.sleep(0.1)
    runner.join()
    assert taken == 0
    assert log == ["start:b1", "end:b1", "start:b2", "end:b2"]
    assert _status(lone, first)[:2] == ("done", 1)
    assert _status(lone, second)[:2] == ("done", 1)
