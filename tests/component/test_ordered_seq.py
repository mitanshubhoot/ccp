"""MT2 (A-104): ordered topics are read by a per-(tenant, topic) seq, never by offset.

The defect: offsets are numbered at insert but visible at commit. Producer A takes offset
10 and commits late; producer B takes 11 and commits first; a reader that saw 11 moved its
cursor past 10, and A's record was never read. Now A holds its topic's counter row until it
commits, B waits for it, and the cursor counts seq, so the reader meets both, in order.

The migration test applies the migrations before 0021 to an empty database, seeds rows the
old way, and checks that 0021 numbers them in offset order and converts each offset cursor.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

from ccp.config import Settings
from ccp.ingest import event_log
from ccp.ingest.consumers import OrderedConsumer
from ccp.ingest.event_log import LogRecord
from ccp.storage import db as storage_db
from ccp.storage.db import MIGRATIONS_DIR, app_pool, migrate, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

pytestmark = pytest.mark.db
TS = datetime(2026, 9, 1, tzinfo=UTC)


class _Recorder(OrderedConsumer):
    topic = "raw.hris"

    def __init__(self, db: ScopedDB, name: str) -> None:
        self.name = name
        super().__init__(db)
        self.seen: list[str] = []

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        self.seen.append(record.ref)


@pytest.fixture
def own(base_settings: Settings) -> Iterator[tuple[Settings, ConnectionPool]]:
    try:
        with scratch_database(base_settings, prefix="ccp_seq") as settings:
            pool = app_pool(settings, max_size=4)
            try:
                yield settings, pool
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


def _append(cur: psycopg.Cursor[Any], tenant: str, topic: str, ref: str) -> int:
    return event_log.append(
        cur, tenant_id=tenant, topic=topic, partition_key="p", source_ts=TS, ref=ref
    )


def _seq(cur: psycopg.Cursor[Any], offset: int) -> int | None:
    record = event_log.get(cur, offset)
    assert record is not None
    return record.seq


def test_a_record_that_commits_late_is_read_not_skipped(
    own: tuple[Settings, ConnectionPool],
) -> None:
    settings, pool = own
    acme = ScopedDB(pool, TenantContext.for_id(settings, "acme"))
    consumer = _Recorder(acme, "seq-recorder")
    a_appended, release_a, b_committed = threading.Event(), threading.Event(), threading.Event()
    offsets: dict[str, int] = {}

    def producer_a() -> None:
        with acme.transaction() as cur:
            offsets["a"] = _append(cur, "acme", "raw.hris", "a")
            a_appended.set()
            release_a.wait(30)

    def producer_b() -> None:
        with acme.transaction() as cur:
            offsets["b"] = _append(cur, "acme", "raw.hris", "b")
        b_committed.set()

    a = threading.Thread(target=producer_a)
    a.start()
    assert a_appended.wait(30)
    b = threading.Thread(target=producer_b)
    b.start()
    try:
        # B waits on the counter row A holds, so it cannot commit a later number first
        assert not b_committed.wait(0.5)
        assert consumer.run_once() == 0  # nothing has committed, so nothing is read
    finally:
        release_a.set()
        a.join(30)
        b.join(30)
    assert b_committed.is_set()
    assert consumer.run_once() == 2
    assert consumer.seen == ["a", "b"]
    with acme.transaction() as cur:
        assert (_seq(cur, offsets["a"]), _seq(cur, offsets["b"])) == (1, 2)
    assert consumer.run_once() == 0


def test_seq_counts_per_tenant_and_ordered_topic_and_skips_parallel_topics(
    own: tuple[Settings, ConnectionPool],
) -> None:
    settings, pool = own
    acme = ScopedDB(pool, TenantContext.for_id(settings, "acme"))
    globex = ScopedDB(pool, TenantContext.for_id(settings, "globex"))
    with acme.transaction() as cur:
        hris = [_append(cur, "acme", "raw.hris", f"h{i}") for i in range(3)]
        github = _append(cur, "acme", "raw.github", "g")
        evaluation = _append(cur, "acme", "evaluations", "e")
    with globex.transaction() as cur:
        theirs = _append(cur, "globex", "raw.hris", "x")
        assert _seq(cur, theirs) == 1
        assert event_log.committed_seq(cur, "raw.hris") == 1
    with acme.transaction() as cur:
        assert [_seq(cur, o) for o in hris] == [1, 2, 3]
        assert _seq(cur, github) == 1
        assert _seq(cur, evaluation) is None
        assert event_log.committed_seq(cur, "raw.hris") == 3


def test_a_claim_takes_at_most_its_limit_on_a_table_without_statistics(
    own: tuple[Settings, ConnectionPool],
) -> None:
    """With no statistics the planner ran `IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)`
    as the inner side of a nested loop and claimed every pending row; the cap broke."""
    settings, pool = own
    acme = ScopedDB(pool, TenantContext.for_id(settings, "acme"))
    with acme.transaction() as cur:
        for i in range(50):
            _append(cur, "acme", "evaluations", f"e{i}")
    with acme.transaction() as cur:
        claimed = event_log.claim(cur, "evaluations", 4, 300)
    assert [r.ref for r in claimed] == ["e0", "e1", "e2", "e3"]
    with acme.transaction() as cur:
        row = cur.execute(
            "SELECT count(*) FROM event_log WHERE topic = 'evaluations' AND status = 'claimed'"
        ).fetchone()
    assert row is not None and row[0] == 4


# -- the migration --------------------------------------------------------------------------


@contextmanager
def _empty_database(settings: Settings) -> Iterator[Settings]:
    maintenance = storage_db._with_dbname(settings.database_admin_url, "postgres")
    admin = psycopg.connect(maintenance, autocommit=True, connect_timeout=3)
    name = f"ccp_mig_{uuid.uuid4().hex[:8]}"
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield settings.model_copy(
            update={
                "database_url": storage_db._with_dbname(settings.database_url, name),
                "database_admin_url": storage_db._with_dbname(settings.database_admin_url, name),
            }
        )
    finally:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def empty(base_settings: Settings) -> Iterator[Settings]:
    try:
        with _empty_database(base_settings) as settings:
            yield settings
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


def _migrate_through(settings: Settings, last: str) -> None:
    """Apply the migration files up to and including `last`, as `migrate` would."""
    role, password = storage_db.app_role_and_password(settings)
    with storage_db.admin_connection(settings.database_admin_url) as conn:
        storage_db._ensure_app_role(conn, role, password)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name > last:
                break
            body = path.read_text(encoding="utf-8").replace("{{app_role}}", role)
            with conn.transaction():
                conn.execute(body)
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))


def test_the_migration_numbers_old_rows_in_offset_order_and_converts_cursors(
    empty: Settings,
) -> None:
    _migrate_through(empty, "0020_rule_versions.sql")
    rows = [
        ("acme", "raw.hris", "a1"),
        ("globex", "raw.hris", "x1"),
        ("acme", "evaluations", "e1"),
        ("acme", "raw.hris", "a2"),
        ("globex", "raw.hris", "x2"),
        ("acme", "raw.hris", "a3"),
    ]
    offsets: dict[str, int] = {}
    with storage_db.admin_connection(empty.database_admin_url) as conn:
        for tenant, topic, ref in rows:
            row = conn.execute(
                "INSERT INTO event_log (tenant_id, topic, partition_key, source_ts, ref)"
                ' VALUES (%s, %s, %s, %s, %s) RETURNING "offset"',
                (tenant, topic, "p", TS, ref),
            ).fetchone()
            assert row is not None
            offsets[ref] = int(row[0])
        conn.execute(
            "INSERT INTO consumer_offsets (consumer, topic, last_offset) VALUES"
            " (%s, 'raw.hris', %s), (%s, 'raw.hris', 0)",
            ("raw-audit:hris:acme", offsets["a2"], "raw-audit:hris:globex"),
        )
    assert migrate(empty)[0] == "0021_ordered_seq.sql"

    with storage_db.admin_connection(empty.database_admin_url) as conn:
        seqs = dict(conn.execute("SELECT ref, seq FROM event_log").fetchall())
        cursors = dict(conn.execute("SELECT consumer, last_seq FROM consumer_offsets").fetchall())
        columns = {
            str(r[0])
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'consumer_offsets'"
            ).fetchall()
        }
    assert seqs == {"a1": 1, "a2": 2, "a3": 3, "x1": 1, "x2": 2, "e1": None}
    assert cursors == {"raw-audit:hris:acme": 2, "raw-audit:hris:globex": 0}
    assert "last_offset" not in columns

    pool = app_pool(empty, max_size=2)
    try:
        acme = ScopedDB(pool, TenantContext.for_id(empty, "acme"))
        with acme.transaction() as cur:
            assert _seq(cur, _append(cur, "acme", "raw.hris", "a4")) == 4
        consumer = _Recorder(acme, "raw-audit:hris")
        assert consumer.run_once() == 2
        assert consumer.seen == ["a3", "a4"]  # the converted cursor resumes after a2
    finally:
        pool.close()
