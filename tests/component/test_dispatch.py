"""MT2 (A-106): the worker serves tenants fairly, wakes on notifications, and runs one
ordered consumer per tenant at a time.

The defects: the loop drained each tenant completely before moving to the next, so one
tenant's backlog held every other tenant's work; it visited every tenant every two seconds
when idle and saw new work only on that tick; and two worker processes running the same
ordered consumer for one tenant both read the same batch, because nothing excluded them.
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
from ccp.ingest import event_log, worker
from ccp.ingest.consumers import OrderedConsumer, ParallelConsumer
from ccp.ingest.event_log import LogRecord
from ccp.storage.db import app_pool, app_role_and_password, scratch_database
from ccp.storage.lint import lint
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

pytestmark = pytest.mark.db
TS = datetime(2026, 9, 1, tzinfo=UTC)
HANDLED: list[tuple[str, str]] = []


class _Recorder(ParallelConsumer):
    name = "dispatch-test"
    topic = "evaluations"

    def __init__(self, db: ScopedDB, settings: Settings) -> None:
        super().__init__(db, visibility_seconds=30, base_delay=0.0)

    def handle(self, record: LogRecord) -> None:
        HANDLED.append((record.tenant_id, record.ref))


class _SlowOrdered(OrderedConsumer):
    name = "dispatch-ordered"
    topic = "raw.hris"

    def __init__(self, db: ScopedDB) -> None:
        super().__init__(db)
        self.seen: list[str] = []

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        time.sleep(0.5)
        self.seen.append(record.ref)


@pytest.fixture
def own(base_settings: Settings) -> Iterator[Settings]:
    try:
        with scratch_database(base_settings, prefix="ccp_disp") as settings:
            yield settings
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


@pytest.fixture
def recorder() -> Iterator[str]:
    HANDLED.clear()
    worker.register("dispatch-test")(lambda db, settings: _Recorder(db, settings))
    try:
        yield "dispatch-test"
    finally:
        worker.CONSUMERS.pop("dispatch-test", None)


def _append(settings: Settings, tenant: str, topic: str, refs: list[str]) -> None:
    pool = app_pool(settings, max_size=1)
    try:
        db = ScopedDB(pool, TenantContext.for_id(settings, tenant))
        with db.transaction() as cur:
            for ref in refs:
                event_log.append(
                    cur, tenant_id=tenant, topic=topic, partition_key=ref, source_ts=TS, ref=ref
                )
    finally:
        pool.close()


def test_a_backlog_in_one_tenant_does_not_hold_another(own: Settings, recorder: str) -> None:
    _append(own, "acme", "evaluations", [f"a{i:02d}" for i in range(60)])
    _append(own, "globex", "evaluations", ["g0"])
    tenants = TenantContext.all(own)
    dispatcher = worker.Dispatcher(own, recorder, tenants, quantum=1)
    try:
        assert dispatcher.run(once=True) == 61
    finally:
        dispatcher.close()
    order = [ref for _, ref in HANDLED]
    # one turn of acme (a claim of 10), then globex: not after all sixty
    assert order.index("g0") <= 10, order[:12]


def test_a_notification_wakes_an_idle_worker(own: Settings, recorder: str) -> None:
    stop = threading.Event()
    tenants = TenantContext.all(own)
    dispatcher = worker.Dispatcher(own, recorder, tenants, sweep_seconds=600)
    loop = threading.Thread(target=lambda: dispatcher.run(once=False, poll_seconds=30, stop=stop))
    loop.start()
    try:
        time.sleep(1.0)  # past the start-up sweep: the worker is idle, waiting
        started = time.monotonic()
        _append(own, "globex", "evaluations", ["late"])
        while ("globex", "late") not in HANDLED:
            assert time.monotonic() - started < 5, "the notification did not wake the worker"
            time.sleep(0.05)
    finally:
        stop.set()
        _append(own, "acme", "evaluations", ["wake"])  # let the waiting loop see the stop
        loop.join(40)
        dispatcher.close()
    assert not loop.is_alive()


def test_one_worker_at_a_time_runs_an_ordered_consumer_for_a_tenant(own: Settings) -> None:
    _append(own, "acme", "raw.hris", ["o1", "o2"])
    pool = app_pool(own, max_size=4)
    try:
        acme = TenantContext.for_id(own, "acme")
        first, second = _SlowOrdered(ScopedDB(pool, acme)), _SlowOrdered(ScopedDB(pool, acme))
        results: dict[str, int] = {}
        a = threading.Thread(target=lambda: results.__setitem__("first", first.run_once()))
        a.start()
        time.sleep(0.2)  # the first holds the consumer's lock inside its batch
        results["second"] = second.run_once()
        a.join(30)
    finally:
        pool.close()
    assert results == {"first": 2, "second": 0}
    assert first.seen == ["o1", "o2"] and second.seen == []


def test_a_failing_tenant_does_not_stop_the_others(own: Settings, recorder: str) -> None:
    class _Failing(_Recorder):
        def run_once(self, limit: int = 10) -> int:
            if self._db.tenant.tenant_id == "acme":
                raise RuntimeError("acme's database is unhappy")
            return super().run_once(limit)

    worker.register("dispatch-test")(lambda db, settings: _Failing(db, settings))
    _append(own, "acme", "evaluations", ["a"])
    _append(own, "globex", "evaluations", ["g"])
    dispatcher = worker.Dispatcher(own, recorder, TenantContext.all(own))
    try:
        dispatcher.run(once=True)
    finally:
        dispatcher.close()
    assert HANDLED == [("globex", "g")]
    assert dispatcher.failures == 1


def test_work_hints_hold_no_tenant_data_and_the_lint_pins_their_columns(own: Settings) -> None:
    role, _ = app_role_and_password(own)
    with psycopg.connect(own.database_admin_url, autocommit=True) as conn:
        assert [str(f) for f in lint(conn, role)] == []
        conn.execute("ALTER TABLE work_hints ADD COLUMN ref TEXT")
        try:
            found = [f for f in lint(conn, role) if f.table == "work_hints"]
            assert found and "columns" in found[0].problem
        finally:
            conn.execute("ALTER TABLE work_hints DROP COLUMN ref")
