"""Connections and the migration runner.

Two roles, two URLs. ``DATABASE_ADMIN_URL`` is used here and by the test database
factory only; application code gets a pool on ``DATABASE_URL``, whose role cannot bypass
row-level security and has no UPDATE or DELETE on append-only tables.

The migration runner only ever adds: files in ``migrations/`` are applied in name order
exactly once and recorded in ``schema_migrations``. A file is never edited after it has
been applied anywhere.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool

from ccp.config import Settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def admin_connection(admin_url: str) -> psycopg.Connection[tuple[object, ...]]:
    """Autocommit connection as the admin role. Migration runner and tests only."""
    return psycopg.connect(admin_url, autocommit=True)


def app_pool(settings: Settings, *, max_size: int = 8) -> ConnectionPool:
    """Pooled connections as the application role: the only route from app code to Postgres."""
    return ConnectionPool(settings.database_url, min_size=1, max_size=max_size, open=True)


AGENT_ROLE = "ccp_agent"
AGENT_STATEMENT_TIMEOUT_MS = 15_000


def agent_url(settings: Settings) -> str:
    """The agent tools' read-only role (A-115): DATABASE_AGENT_URL, or the application URL
    with the user `ccp_agent`."""
    return settings.database_agent_url or make_conninfo(settings.database_url, user=AGENT_ROLE)


def agent_pool(settings: Settings, *, max_size: int = 4) -> ConnectionPool:
    """Pooled connections as the read-only role agent tools run on: SELECT on what tools
    read and nothing else, under row-level security, every statement time-limited."""
    conninfo = make_conninfo(
        agent_url(settings), options=f"-c statement_timeout={AGENT_STATEMENT_TIMEOUT_MS}"
    )
    return ConnectionPool(conninfo, min_size=1, max_size=max_size, open=True)


class Pools:
    """Pools a run opens together and closes together (its checkpoints, its tools)."""

    def __init__(self, *pools: ConnectionPool) -> None:
        self._pools = pools

    def close(self) -> None:
        for pool in self._pools:
            pool.close()


def app_role_and_password(settings: Settings) -> tuple[str, str | None]:
    return _role_and_password(settings.database_url, "DATABASE_URL")


def agent_role_and_password(settings: Settings) -> tuple[str, str | None]:
    return _role_and_password(agent_url(settings), "DATABASE_AGENT_URL")


def _role_and_password(url: str, name: str) -> tuple[str, str | None]:
    info = conninfo_to_dict(url)
    user = info.get("user")
    if not isinstance(user, str) or not user:
        raise ValueError(f"{name} must name a role")
    password = info.get("password")
    return user, password if isinstance(password, str) else None


def _ensure_app_role(
    conn: psycopg.Connection[tuple[object, ...]], role: str, password: str | None
) -> None:
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    if exists is None:
        stmt = sql.SQL(
            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}"
        ).format(sql.Identifier(role), sql.Literal(password))
        conn.execute(stmt)
    else:
        # Idempotent hardening: the app role must never be able to bypass RLS.
        conn.execute(sql.SQL("ALTER ROLE {} NOBYPASSRLS NOSUPERUSER").format(sql.Identifier(role)))
    dbname = conn.info.dbname
    conn.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(dbname), sql.Identifier(role)
        )
    )


def migrate(settings: Settings) -> list[str]:
    """Apply pending migrations as the admin role. Returns the names applied this run."""
    role, password = app_role_and_password(settings)
    agent_role, agent_password = agent_role_and_password(settings)
    applied: list[str] = []
    with admin_connection(settings.database_admin_url) as conn:
        _ensure_app_role(conn, role, password)
        _ensure_app_role(conn, agent_role, agent_password)  # the same hardening: A-115
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        # The runner owns this table, so it grants read access here on every run rather
        # than in a migration file, which is never edited after it has been applied.
        conn.execute(
            sql.SQL("GRANT SELECT ON schema_migrations TO {}").format(sql.Identifier(role))
        )
        done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            body = (
                path.read_text(encoding="utf-8")
                .replace("{{app_role}}", role)
                .replace("{{agent_role}}", agent_role)
            )
            with conn.transaction():
                # No parameters, so psycopg sends the file with the simple query
                # protocol, which allows several statements in one call.
                conn.execute(body)
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
        has_checkpoints = conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = 'checkpoints'"
        ).fetchone()
    if has_checkpoints is not None:
        # LangGraph's saver creates its own tables; run its idempotent setup as the admin
        # role every time, then apply row-level security on thread_id (spec.md R8, S14).
        from ccp.storage.checkpoints import setup_checkpoint_tables

        setup_checkpoint_tables(settings.database_admin_url, role)
    return applied


def _with_dbname(url: str, dbname: str) -> str:
    return make_conninfo(url, dbname=dbname)


@contextmanager
def scratch_database(settings: Settings, prefix: str = "ccp_scratch") -> Iterator[Settings]:
    """Create a throwaway database, migrate it, yield settings pointing at it, drop it.

    Used by the test suite (one per session) and by the eval runner (one per run) so
    neither ever touches the working database. Needs the admin URL to reach the server.
    """
    maintenance_url = _with_dbname(settings.database_admin_url, "postgres")
    admin = psycopg.connect(maintenance_url, autocommit=True, connect_timeout=3)
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    scoped = settings.model_copy(
        update={
            "database_url": _with_dbname(settings.database_url, name),
            "database_admin_url": _with_dbname(settings.database_admin_url, name),
            "database_agent_url": (
                _with_dbname(settings.database_agent_url, name)
                if settings.database_agent_url
                else None
            ),
        }
    )
    try:
        migrate(scoped)
        yield scoped
    finally:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def lint_catalogue(settings: Settings) -> list[Any]:
    """The row-level security catalogue lint (ccp/storage/lint.py), as the admin role: only
    the migration runner reaches the admin URL."""
    from ccp.storage.lint import lint

    role, _ = app_role_and_password(settings)
    agent, _ = agent_role_and_password(settings)
    with psycopg.connect(settings.database_admin_url) as conn:
        return list(lint(conn, role, agent))


def check(settings: Settings) -> dict[str, str]:
    """Connect as the application role and report what the server looks like."""
    with psycopg.connect(settings.database_url) as conn:
        version = conn.execute("SELECT version()").fetchone()
        vector = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        role = conn.execute(
            "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        migrations = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
    return {
        "server": str(version[0]) if version else "unknown",
        "pgvector": str(vector[0]) if vector else "missing",
        "role_bypassrls": str(role[0]) if role else "unknown",
        "role_superuser": str(role[1]) if role else "unknown",
        "migrations_applied": str(migrations[0]) if migrations else "0",
    }
