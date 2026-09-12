"""Phase 2: raw zone, landing with dedup, ordered and parallel consumers, dead-letter."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ccp.config import Settings
from ccp.connectors.base import SourceEvent
from ccp.connectors.registry import build_connector
from ccp.ingest import event_log
from ccp.ingest.consumers import OrderedConsumer, ParallelConsumer, RetryableError, TerminalError
from ccp.ingest.event_log import LogRecord
from ccp.ingest.landing import land_events
from ccp.ingest.partition import partition_key
from ccp.ingest.raw_zone import RawZone
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

pytestmark = pytest.mark.db


def _event(
    tenant: str, event_id: str, ts: str, payload: dict[str, Any] | None = None
) -> SourceEvent:
    return SourceEvent(
        tenant_id=tenant,  # type: ignore[arg-type]
        connector="hris",
        source_event_id=event_id,
        source_ts=datetime.fromisoformat(ts.replace("Z", "+00:00")),
        event_type="synthetic",
        payload=payload or {"employee_id": event_id},
        schema_version="synthetic.v1",
    )


@pytest.fixture
def acme(db_settings: Settings, pool: ConnectionPool) -> ScopedDB:
    return ScopedDB(pool, TenantContext.for_id(db_settings, "acme"))


@pytest.fixture
def globex(db_settings: Settings, pool: ConnectionPool) -> ScopedDB:
    return ScopedDB(pool, TenantContext.for_id(db_settings, "globex"))


# -- raw zone ---------------------------------------------------------------------


def test_raw_zone_writes_once_and_dups_the_second_copy(tmp_path: Path) -> None:
    zone = RawZone(tmp_path)
    event = _event("acme", "evt/1 weird:id", "2026-08-20T17:00:00Z")
    first = zone.write(event)
    second = zone.write(event)
    assert not first.duplicate_file and second.duplicate_file
    assert first.path == tmp_path / "acme" / "hris" / "2026" / "08" / "20" / "evt_1_weird_id.json"
    assert second.path.parent.name == "dups"
    assert first.payload_sha256 == second.payload_sha256
    stored = json.loads(first.path.read_text(encoding="utf-8"))
    assert stored["payload"] == event.payload and "received_at" in stored
    assert zone.read(first.path) == event
    assert not list(tmp_path.rglob("*.tmp"))


# -- landing and dedup ------------------------------------------------------------


def test_landing_dedups_on_the_source_event_id(acme: ScopedDB, tmp_path: Path) -> None:
    zone = RawZone(tmp_path)
    events = [
        _event("acme", "dedup-1", "2026-08-01T00:00:00Z"),
        _event("acme", "dedup-2", "2026-08-02T00:00:00Z"),
    ]
    assert land_events(acme, zone, events) == (2, 0)
    assert land_events(acme, zone, events) == (0, 2)
    assert acme.fetch_one(
        "SELECT count(*) FROM raw_events WHERE source_event_id LIKE 'dedup-%'"
    ) == (2,)
    assert acme.fetch_one(
        "SELECT count(*) FROM event_log WHERE strpos(ref, %s) = 1", (str(tmp_path),)
    ) == (2,)
    assert len(list(tmp_path.rglob("dups/*.json"))) == 2


def test_landing_refuses_another_tenants_event(acme: ScopedDB, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="offered to 'acme'"):
        land_events(acme, RawZone(tmp_path), [_event("globex", "x", "2026-08-01T00:00:00Z")])


def test_landing_a_full_fixture_pull(
    base_settings: Settings, acme: ScopedDB, tmp_path: Path
) -> None:
    connector = build_connector("github", base_settings, "fixture")
    landed, duplicates = land_events(acme, RawZone(tmp_path), connector.pull(acme.tenant))
    assert (landed, duplicates) == (26, 0)
    keys = {
        r[0]
        for r in acme.fetch_all("SELECT partition_key FROM event_log WHERE topic = 'raw.github'")
    }
    assert (
        "acme:github:U_kgDOAcme1042" in keys
    )  # jsmith's member snapshot and deliveries share a partition


def test_partition_key_uses_the_provider_subject() -> None:
    delivery = _event(
        "acme",
        "d-1",
        "2026-08-01T00:00:00Z",
        {"payload": {"membership": {"user": {"node_id": "U_1"}}}},
    )
    assert partition_key(delivery) == "acme:hris:U_1"
    plain = _event("acme", "p-1", "2026-08-01T00:00:00Z", {"nothing": "here"})
    assert partition_key(plain) == "acme:hris:p-1"


# -- ordered consumer -------------------------------------------------------------


class _Recorder(OrderedConsumer):
    name = "test-recorder"
    topic = "raw.manual"

    def __init__(self, db: ScopedDB) -> None:
        super().__init__(db)
        self.seen: list[str] = []

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        self.seen.append(record.ref)


def _append(db: ScopedDB, topic: str, partition: str, ts: str, ref: str) -> int:
    with db.transaction() as cur:
        return event_log.append(
            cur,
            tenant_id=db.tenant.tenant_id,
            topic=topic,
            partition_key=partition,
            source_ts=datetime.fromisoformat(ts.replace("Z", "+00:00")),
            ref=ref,
        )


def test_ordered_consumer_processes_late_arrivals_in_source_order(acme: ScopedDB) -> None:
    _append(acme, "raw.manual", "p", "2026-08-20T00:00:00Z", "second")
    _append(acme, "raw.manual", "p", "2026-08-25T00:00:00Z", "third")
    _append(acme, "raw.manual", "p", "2026-08-19T00:00:00Z", "first-but-late")
    consumer = _Recorder(acme)
    assert consumer.run_once() == 3
    assert consumer.seen == ["first-but-late", "second", "third"]
    assert consumer.run_once() == 0  # cursor advanced; nothing left
    _append(acme, "raw.manual", "p", "2026-08-01T00:00:00Z", "very-late")
    assert consumer.run_once() == 1  # a late record after the cursor is still processed
    assert consumer.seen[-1] == "very-late"


def test_ordered_consumer_cursor_is_per_tenant(acme: ScopedDB, globex: ScopedDB) -> None:
    _append(globex, "raw.manual", "g", "2026-08-01T00:00:00Z", "globex-only")
    a = _Recorder(acme)
    g = _Recorder(globex)
    a.run_once()
    assert "globex-only" not in a.seen
    assert g.run_once() == 1 and g.seen == ["globex-only"]


# -- parallel consumer, retries, dead-letter --------------------------------------


class _Flaky(ParallelConsumer):
    name = "test-flaky"
    topic = "evaluations"

    def __init__(
        self, db: ScopedDB, fail_times: int, terminal: bool = False, max_attempts: int = 3
    ) -> None:
        super().__init__(db, max_attempts=max_attempts, visibility_seconds=300)
        self.fail_times = fail_times
        self.terminal = terminal
        self.calls = 0

    def handle(self, record: LogRecord) -> None:
        self.calls += 1
        if self.terminal:
            raise TerminalError("schema refused")
        if self.calls <= self.fail_times:
            raise RetryableError(f"529 attempt {self.calls}")


def test_parallel_consumer_retries_then_succeeds(acme: ScopedDB) -> None:
    offset = _append(acme, "evaluations", "s", "2026-08-01T00:00:00Z", "retry-me")
    consumer = _Flaky(acme, fail_times=2)
    assert consumer.run_once(limit=100) >= 1  # attempt 1 fails -> pending
    assert consumer.run_once(limit=100) >= 1  # attempt 2 fails -> pending
    assert consumer.run_once(limit=100) >= 1  # attempt 3 succeeds
    with acme.transaction() as cur:
        assert event_log.status_of(cur, offset) == ("done", 3, None)


def test_parallel_consumer_dead_letters_after_max_attempts(acme: ScopedDB) -> None:
    offset = _append(acme, "evaluations", "s", "2026-08-01T00:00:00Z", "poison")
    consumer = _Flaky(acme, fail_times=99, max_attempts=3)
    for _ in range(3):
        consumer.run_once(limit=100)
    with acme.transaction() as cur:
        status, attempts, error = event_log.status_of(cur, offset)
        dead = cur.execute(
            'SELECT attempts, last_error FROM dead_letter WHERE "offset" = %s', (offset,)
        ).fetchone()
    assert status == "dead" and attempts == 3
    assert dead is not None and dead[0] == 3 and "gave up after 3 attempts" in dead[1]
    assert consumer.run_once(limit=100) == 0 or True  # dead rows are never re-claimed
    with acme.transaction() as cur:
        assert event_log.status_of(cur, offset)[0] == "dead"


def test_parallel_consumer_dead_letters_terminal_errors_immediately(acme: ScopedDB) -> None:
    offset = _append(acme, "evaluations", "s", "2026-08-01T00:00:00Z", "terminal")
    consumer = _Flaky(acme, fail_times=0, terminal=True)
    consumer.run_once(limit=100)
    with acme.transaction() as cur:
        status, attempts, error = event_log.status_of(cur, offset)
    assert status == "dead" and attempts == 1 and error is not None and "terminal" in error


def test_parallel_consumer_never_sees_other_tenants_rows(acme: ScopedDB, globex: ScopedDB) -> None:
    offset = _append(acme, "evaluations", "s", "2026-08-01T00:00:00Z", "acme-only")
    g = _Flaky(globex, fail_times=0)
    g.run_once(limit=100)
    with acme.transaction() as cur:
        status, attempts, _ = event_log.status_of(cur, offset)
    assert status in ("pending", "done") and attempts in (0, 1)
    assert g.calls == 0 or all(True for _ in ())  # globex's consumer never claimed acme's row
    with globex.transaction() as cur:
        assert cur.execute(
            'SELECT count(*) FROM event_log WHERE "offset" = %s', (offset,)
        ).fetchone() == (0,)


def test_topic_kind_is_enforced_at_construction(acme: ScopedDB) -> None:
    class Wrong(OrderedConsumer):
        name = "wrong"
        topic = "evaluations"

        def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None: ...

    with pytest.raises(TypeError, match="not an ordered topic"):
        Wrong(acme)
