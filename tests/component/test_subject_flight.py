"""MT2 (A-107): one evaluation in flight per subject, an atomic concurrency cap, and late
verdicts filed into history.

The defects: claims ignored the partition key, so two workers could evaluate one subject at
once; the cap was read in one transaction and claimed in another, so workers that read the
same room filled it more than once; and the state writer made whichever verdict arrived last
the head of the chain, so a stale evaluation that finished late replaced a newer one.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ccp.config import Settings
from ccp.ingest import event_log
from ccp.ingest.consumers import ParallelConsumer
from ccp.ingest.event_log import LogRecord
from ccp.storage import bitemporal
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from ccp.types import VerdictStatus
from tests.component.test_bitemporal import stub_verdict

pytestmark = pytest.mark.db
TS = datetime(2026, 9, 1, tzinfo=UTC)
MAR = {d: datetime(2026, 3, d, 12, 0, tzinfo=UTC) for d in range(1, 15)}


@dataclass
class _Active:
    lock: threading.Lock = field(default_factory=threading.Lock)
    now: int = 0
    peak: int = 0
    keys: dict[str, int] = field(default_factory=dict)
    overlaps: int = 0
    handled: list[str] = field(default_factory=list)


class _Keyed(ParallelConsumer):
    name = "flight-test"
    topic = "evaluations"
    one_per_key = True

    def __init__(self, db: ScopedDB, state: _Active, cap: int | None, sleep: float) -> None:
        super().__init__(db, visibility_seconds=30, base_delay=0.0, max_in_flight=cap)
        self.state = state
        self.sleep = sleep

    def handle(self, record: LogRecord) -> None:
        s = self.state
        with s.lock:
            s.now += 1
            s.peak = max(s.peak, s.now)
            s.keys[record.partition_key] = s.keys.get(record.partition_key, 0) + 1
            if s.keys[record.partition_key] > 1:
                s.overlaps += 1
        time.sleep(self.sleep)
        with s.lock:
            s.now -= 1
            s.keys[record.partition_key] -= 1
            s.handled.append(record.ref)


@pytest.fixture
def own(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with scratch_database(base_settings, prefix="ccp_flight") as settings:
            pool = app_pool(settings, max_size=20)
            try:
                yield settings, ScopedDB(pool, TenantContext.for_id(settings, "acme"))
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


@pytest.fixture
def lone(own: tuple[Settings, ScopedDB]) -> ScopedDB:
    return own[1]


def _append(db: ScopedDB, key: str, ref: str) -> int:
    with db.transaction() as cur:
        return event_log.append(
            cur, tenant_id="acme", topic="evaluations", partition_key=key, source_ts=TS, ref=ref
        )


def _claim(db: ScopedDB, owner: str) -> list[LogRecord]:
    with db.transaction() as cur:
        return event_log.claim(cur, "evaluations", 10, 30.0, owner, one_per_key=True)


def test_two_records_of_one_subject_are_never_claimed_together(lone: ScopedDB) -> None:
    _append(lone, "acme:HR-1|jane", "jane-1")
    _append(lone, "acme:HR-1|jane", "jane-2")
    _append(lone, "acme:HR-1|raj", "raj-1")
    first = _claim(lone, "worker-a")
    assert [r.ref for r in first] == ["jane-1", "raj-1"]  # one per subject, the oldest first
    assert _claim(lone, "worker-b") == []  # jane-2 waits behind jane-1's live lease
    with lone.transaction() as cur:
        assert event_log.mark_done(cur, first[0].offset, lease=first[0].lease_token)
    assert [r.ref for r in _claim(lone, "worker-b")] == ["jane-2"]


def test_concurrent_workers_never_run_one_subject_twice_or_pass_the_cap(lone: ScopedDB) -> None:
    for i in range(24):
        _append(lone, f"acme:k{i % 4}", f"r{i:02d}")
    state = _Active()

    def work() -> None:
        consumer = _Keyed(lone, state, cap=3, sleep=0.05)
        idle = 0
        while idle < 8:
            if consumer.run_once(limit=2):
                idle = 0
            else:
                idle += 1
                time.sleep(0.05)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert sorted(state.handled) == [f"r{i:02d}" for i in range(24)]
    assert state.overlaps == 0
    assert 1 <= state.peak <= 3


def test_a_record_waiting_behind_its_subject_is_not_due_and_is_woken(
    own: tuple[Settings, ScopedDB],
) -> None:
    settings, lone = own
    _append(lone, "acme:HR-1|jane", "jane-1")
    _append(lone, "acme:HR-1|jane", "jane-2")
    (held,) = _claim(lone, "worker-a")
    waiting = _Keyed(lone, _Active(), cap=None, sleep=0.0)
    due = waiting.next_due()  # bounded by jane-1's lease, not "now"
    assert due is not None and due > datetime.now(UTC) + timedelta(seconds=20)
    with lone.transaction() as cur:  # jane-1's worker died: its lease runs out
        cur.execute(
            "UPDATE event_log SET lease_expires_at = now() - interval '1 second'"
            ' WHERE "offset" = %s',
            (held.offset,),
        )
    with psycopg.connect(settings.database_url, autocommit=True) as listener:
        listener.execute(f"LISTEN {event_log.CHANNEL}")
        assert waiting.run_once() == 1  # re-claims jane-1, the oldest, and finishes it
        notes = [n.payload for n in listener.notifies(timeout=2, stop_after=1)]
    assert notes == ["acme:evaluations"]  # jane-2 is waiting, so its workers are woken
    assert [r.ref for r in _claim(lone, "worker-b")] == ["jane-2"]


# -- late verdicts ---------------------------------------------------------------------------


def _record(db: ScopedDB, subject: str, vid: str, status: str, valid_from: datetime, **kw: object):  # type: ignore[no-untyped-def]
    stub_verdict(db, vid, "HR-1", subject, status)
    return bitemporal.record(
        db,
        subject_key=subject,
        test_id="HR-1",
        control_ids=("CTL-ACCESS-REVOKE",),
        status=status,
        verdict_id=vid,
        valid_from=valid_from,
        **kw,  # type: ignore[arg-type]
    )


def test_a_late_fact_is_filed_into_history_and_the_newer_head_stays(lone: ScopedDB) -> None:
    subject = "HR-1|person:late"
    newer = _record(lone, subject, "v-late-1", "fail", MAR[10], late_into_history=True)
    late = _record(lone, subject, "v-late-2", "pass", MAR[4], late_into_history=True)
    assert late.history and late.supersedes_row_id is None
    assert late.valid_to == MAR[10] and late.reason == "late_event"
    head = bitemporal.current_state(lone, subject, MAR[11])
    assert head is not None and head.row_id == newer.row_id
    assert head.status is VerdictStatus.failed
    then = bitemporal.state_as_of(lone, subject, MAR[5], datetime.now(UTC))
    assert then is not None and then.row_id == late.row_id  # the past it describes
    assert [r.row_id for r in bitemporal.heads(lone) if r.subject_key == subject] == [newer.row_id]
    # a newer fact still supersedes the head, as before
    newest = _record(lone, subject, "v-late-3", "pass", MAR[12], late_into_history=True)
    assert newest.supersedes_row_id == newer.row_id and not newest.history
    # a correction without the flag may still rewrite the past from an earlier moment
    fix = _record(lone, subject, "v-late-4", "fail", MAR[1], reason="correction")
    assert fix.supersedes_row_id == newest.row_id and not fix.history
    now = bitemporal.current_state(lone, subject, MAR[13])
    assert now is not None and now.row_id == fix.row_id
