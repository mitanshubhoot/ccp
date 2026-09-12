"""LangGraph checkpoint tables under row-level security (spec.md Layer 8, R8, S14).

    migration runner (admin)     setup_checkpoint_tables(): PostgresSaver.setup() with
                                 search_path=checkpoints, then FORCE RLS + a thread_id policy
    tier 3 wrapper (app role)    saver_pool(settings, tenant): a psycopg pool whose sessions
                                 carry search_path=checkpoints and app.tenant_id=<tenant>, so
                                 every saver query is scoped without a SET LOCAL per statement

LangGraph owns the tables' shape; they carry `thread_id`, and every thread id this code
mints starts with the tenant id, so the policy is `thread_id LIKE app_tenant() || ':%'`.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ccp.config import Settings
from ccp.tenancy import TenantContext

SCHEMA = "checkpoints"
THREAD_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def setup_checkpoint_tables(admin_url: str, app_role: str) -> None:
    """Create or migrate the saver's tables inside the checkpoints schema, then lock them."""
    from langgraph.checkpoint.postgres import PostgresSaver

    conn: psycopg.Connection[Any] = psycopg.connect(
        admin_url,
        autocommit=True,
        row_factory=dict_row,
        options=f"-c search_path={SCHEMA}",
    )
    with conn:
        PostgresSaver(conn).setup()
        role = sql.Identifier(app_role)
        conn.execute(
            sql.SQL("GRANT SELECT ON {}.checkpoint_migrations TO {}").format(
                sql.Identifier(SCHEMA), role
            )
        )
        for name in THREAD_TABLES:
            table = sql.Identifier(SCHEMA, name)
            conn.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(table, role)
            )
            conn.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(table))
            conn.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(table))
            conn.execute(sql.SQL("DROP POLICY IF EXISTS tenant_isolation ON {}").format(table))
            conn.execute(
                sql.SQL(
                    "CREATE POLICY tenant_isolation ON {} USING"
                    " (thread_id LIKE public.app_tenant() || ':%%')"
                    " WITH CHECK (thread_id LIKE public.app_tenant() || ':%%')"
                ).format(table)
            )


def saver_conninfo(settings: Settings, tenant: TenantContext) -> str:
    return make_conninfo(
        settings.database_url,
        options=f"-c search_path={SCHEMA} -c app.tenant_id={tenant.tenant_id}",
    )


def saver_pool(settings: Settings, tenant: TenantContext, *, max_size: int = 4) -> ConnectionPool:
    """The saver needs autocommit, no prepared statements, and dict rows."""
    return ConnectionPool(
        saver_conninfo(settings, tenant),
        min_size=1,
        max_size=max_size,
        open=True,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )


def thread_id_for(tenant: TenantContext, test_id: str, subject_key: str, run_id: str) -> str:
    """Every thread id starts with the tenant id; the RLS policy and the CLI guard rely on it."""
    return f"{tenant.tenant_id}:{test_id}:{subject_key}:{run_id}"


def tenant_of_thread(thread_id: str) -> str:
    return thread_id.split(":", 1)[0]
