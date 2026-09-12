"""Phase 0 database tests: migrations, the app role, and row-level security.

These are the first form of the S1 and S15 guarantees (spec.md Layer 13): the database
itself keeps tenants apart, and an unscoped read raises rather than returning empty.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ccp.config import Settings
from ccp.storage.db import migrate
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

pytestmark = pytest.mark.db

INSERT = (
    "INSERT INTO raw_events"
    " (tenant_id, connector, source_event_id, source_ts, raw_path, payload_sha256)"
    " VALUES (%s, %s, %s, now(), %s, %s)"
)


def test_migrations_are_recorded_and_idempotent(db_settings: Settings) -> None:
    assert migrate(db_settings) == []  # session fixture already applied them
    with psycopg.connect(db_settings.database_admin_url) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name")]
        tables = {
            r[0]
            for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
    assert names[:2] == ["0001_schema.sql", "0002_rls.sql"]
    assert names == sorted(names)  # applied in name order, every file recorded once
    assert {"raw_events", "event_log", "consumer_offsets", "dead_letter"} <= tables


def test_app_role_cannot_bypass_rls(db_settings: Settings) -> None:
    with psycopg.connect(db_settings.database_url) as conn:
        row = conn.execute(
            "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    assert row == (False, False)


def test_rls_is_enabled_and_forced_on_tenant_tables(db_settings: Settings) -> None:
    with psycopg.connect(db_settings.database_admin_url) as conn:
        rows = conn.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class"
            " WHERE relname IN ('raw_events', 'event_log', 'dead_letter') ORDER BY relname"
        ).fetchall()
    assert rows == [
        ("dead_letter", True, True),
        ("event_log", True, True),
        ("raw_events", True, True),
    ]


def test_unscoped_read_raises_rather_than_returning_empty(db_settings: Settings) -> None:
    with psycopg.connect(db_settings.database_url) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="app.tenant_id is not set"):
            conn.execute("SELECT count(*) FROM raw_events").fetchone()


def test_setting_from_a_previous_transaction_does_not_leak(db_settings: Settings) -> None:
    """After SET LOCAL the parameter exists on the session and reads as ''. It must still raise."""
    with psycopg.connect(db_settings.database_url) as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', 'acme', true)")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("SELECT count(*) FROM raw_events").fetchone()


def test_scoped_db_sees_only_its_tenant(db_settings: Settings, pool: ConnectionPool) -> None:
    acme = ScopedDB(pool, TenantContext.for_id(db_settings, "acme"))
    globex = ScopedDB(pool, TenantContext.for_id(db_settings, "globex"))
    acme.execute(INSERT, ("acme", "github", "evt-1", "data/raw/acme/x.json", "a" * 64))
    globex.execute(INSERT, ("globex", "github", "evt-1", "data/raw/globex/x.json", "b" * 64))
    globex.execute(INSERT, ("globex", "hris", "evt-2", "data/raw/globex/y.json", "c" * 64))

    assert acme.fetch_one("SELECT count(*) FROM raw_events") == (1,)
    assert globex.fetch_one("SELECT count(*) FROM raw_events") == (2,)
    assert acme.fetch_all("SELECT tenant_id FROM raw_events") == [("acme",)]
    # No WHERE clause anywhere above: the database did the scoping.


def test_scoped_db_cannot_write_another_tenants_row(
    db_settings: Settings, pool: ConnectionPool
) -> None:
    acme = ScopedDB(pool, TenantContext.for_id(db_settings, "acme"))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        acme.execute(INSERT, ("globex", "github", "evt-9", "data/raw/globex/z.json", "d" * 64))


def test_append_only_tables_reject_update_and_delete(
    db_settings: Settings, pool: ConnectionPool
) -> None:
    acme = ScopedDB(pool, TenantContext.for_id(db_settings, "acme"))
    acme.execute(INSERT, ("acme", "hris", "evt-3", "data/raw/acme/y.json", "e" * 64))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        acme.execute("UPDATE raw_events SET raw_path = 'x' WHERE source_event_id = 'evt-3'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        acme.execute("DELETE FROM raw_events WHERE source_event_id = 'evt-3'")


def test_pgvector_extension_is_installed(db_settings: Settings) -> None:
    with psycopg.connect(db_settings.database_url) as conn:
        row = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    assert row is not None
