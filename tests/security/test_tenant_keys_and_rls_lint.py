"""NT9 (A-99): the database keeps every reference inside one tenant.

- The catalogue lint passes on the migrated schema, covering every tenant table, and fails
  on its negative control: a table opened by a deliberately permissive policy.
- A reference to another tenant's row is rejected by the database itself, even though
  foreign-key checks bypass row-level security.
- The caches key by tenant: two tenants caching the same text each keep their own row.
- The checkpoint saver scopes each statement by a transaction-local tenant setting.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from ulid import ULID

from ccp.config import Settings
from ccp.retrieval import cache
from ccp.storage import checkpoints
from ccp.storage.db import app_pool, app_role_and_password, scratch_database
from ccp.storage.lint import DISPATCH, INSERT_ONLY, OPERATIONAL, lint
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def migrated(base_settings: Settings) -> Iterator[Settings]:
    try:
        with scratch_database(base_settings, prefix="ccp_nt9") as settings:
            yield settings
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


def test_the_lint_passes_and_covers_every_tenant_table(migrated: Settings) -> None:
    role, _ = app_role_and_password(migrated)
    with psycopg.connect(migrated.database_admin_url) as conn:
        assert [str(f) for f in lint(conn, role)] == []
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " JOIN pg_attribute a ON a.attrelid = c.oid"
                " WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attname = 'tenant_id'"
            ).fetchall()
        }
    # every tenant table is classified
    assert tables and tables <= INSERT_ONLY | OPERATIONAL | set(DISPATCH)


def test_a_permissive_policy_fails_the_lint(migrated: Settings) -> None:
    role, _ = app_role_and_password(migrated)
    with psycopg.connect(migrated.database_admin_url, autocommit=True) as conn:
        conn.execute("CREATE TABLE lint_control (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL)")
        try:
            conn.execute("ALTER TABLE lint_control ENABLE ROW LEVEL SECURITY")
            conn.execute("ALTER TABLE lint_control FORCE ROW LEVEL SECURITY")
            conn.execute(
                "CREATE POLICY tenant_isolation ON lint_control"
                " USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant())"
            )
            conn.execute("CREATE POLICY wide_open ON lint_control USING (true)")
            found = [f for f in lint(conn, role) if f.table == "lint_control"]
            problems = " | ".join(f.problem for f in found)
            assert "not exactly ['tenant_isolation']" in problems
            assert "unclassified" in problems
            conn.execute("ALTER TABLE lint_control NO FORCE ROW LEVEL SECURITY")
            found = [f for f in lint(conn, role) if f.table == "lint_control"]
            assert any("not enabled and forced" in f.problem for f in found)
        finally:
            conn.execute("DROP TABLE lint_control")
        conn.execute("GRANT UPDATE ON verdicts TO " + role)  # an audit table opened up
        try:
            assert any(
                f.table == "verdicts" and "UPDATE is granted" in f.problem for f in lint(conn, role)
            )
        finally:
            conn.execute("REVOKE UPDATE ON verdicts FROM " + role)


def _verdict(cur: psycopg.Cursor, tenant: str) -> str:  # type: ignore[type-arg]
    verdict_id = str(ULID())
    cur.execute(
        "INSERT INTO verdicts (verdict_id, evaluation_key, tenant_id, test_id, subject_key,"
        " status, reasoning, confidence, confidence_cap, tier, trigger_observed_at, valid_from)"
        " VALUES (%s, %s, %s, 'HR-1', 'HR-1|x', 'pass', 'r', 1, 1, 1, %s, %s)",
        (verdict_id, f"key-{verdict_id}", tenant, NOW, NOW),
    )
    return verdict_id


def test_a_reference_to_another_tenants_row_is_rejected(migrated: Settings) -> None:
    pool = app_pool(migrated, max_size=2)
    try:
        globex = ScopedDB(pool, TenantContext.for_id(migrated, "globex"))
        acme = ScopedDB(pool, TenantContext.for_id(migrated, "acme"))
        with globex.transaction() as cur:
            theirs = _verdict(cur, "globex")
        with acme.transaction() as cur:
            mine = _verdict(cur, "acme")

        def state_row(verdict_id: str) -> None:
            with acme.transaction() as cur:
                cur.execute(
                    "INSERT INTO compliance_state (row_id, tenant_id, subject_key, test_id,"
                    " status, verdict_id, valid_from, recorded_seq, recorded_at, reason)"
                    " VALUES (%s, 'acme', 'HR-1|x', 'HR-1', 'pass', %s, %s, %s, %s,"
                    " 'evaluation')",
                    (str(ULID()), verdict_id, NOW, int(ULID().milliseconds), NOW),
                )

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            state_row(theirs)  # globex's verdict id, from an acme row
        state_row(mine)
        with pytest.raises(psycopg.errors.ForeignKeyViolation), acme.transaction() as cur:
            cur.execute(
                "INSERT INTO remediation_proposals (proposal_id, tenant_id, verdict_id, source,"
                " action, target, rationale, reversible, live_capable, payload_sha256)"
                " VALUES (%s, 'acme', %s, 'rule', 'notify_owner', '{}', 'r', true, false, 'h')",
                (str(ULID()), theirs),
            )
    finally:
        pool.close()


def test_the_caches_are_keyed_by_tenant(migrated: Settings) -> None:
    pool = app_pool(migrated, max_size=2)
    try:
        acme = ScopedDB(pool, TenantContext.for_id(migrated, "acme"))
        globex = ScopedDB(pool, TenantContext.for_id(migrated, "globex"))
        key = cache.embedding_key("local-hashing-v1", "document", "the same text")
        cache.put_embeddings(acme, "local-hashing-v1", {key: [0.1] * 1024})
        cache.put_embeddings(globex, "local-hashing-v1", {key: [0.2] * 1024})  # no collision
        assert cache.get_embeddings(acme, [key])[key][0] == pytest.approx(0.1)
        assert cache.get_embeddings(globex, [key])[key][0] == pytest.approx(0.2)
        cache.put_enrichment(acme, "k", "claude-haiku-4-5", "v1", "acme context")
        cache.put_enrichment(globex, "k", "claude-haiku-4-5", "v1", "globex context")
        assert cache.get_enrichment(acme, "k") == "acme context"
        assert cache.get_enrichment(globex, "k") == "globex context"
    finally:
        pool.close()


def test_the_saver_scopes_each_statement_by_a_transaction_local_tenant(
    migrated: Settings,
) -> None:
    assert "app.tenant_id" not in checkpoints.saver_conninfo(
        migrated, TenantContext.for_id(migrated, "acme")
    )  # no session-wide tenant on the connection
    pool = checkpoints.saver_pool(migrated, TenantContext.for_id(migrated, "acme"))
    try:
        saver = checkpoints.tenant_saver(pool, TenantContext.for_id(migrated, "acme"))
        config = {
            "configurable": {"thread_id": "acme:HR-4:HR-4|tenant:01TEST", "checkpoint_ns": ""}
        }
        assert saver.get_tuple(config) is None  # runs, scoped, with nothing stored yet
        with pool.connection() as conn:
            row = conn.execute("SELECT current_setting('app.tenant_id', true) AS tenant").fetchone()
        assert row is not None and not row["tenant"]  # nothing lingers on the connection
    finally:
        pool.close()
