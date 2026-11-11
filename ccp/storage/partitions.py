"""Partitions for the largest tenants (spec.md A-172).

A large table can be partitioned by tenant: the parent keeps its name, its grants, its
row-level security and its policies, every tenant's rows go to a default partition, and a
tenant that outgrows the rest gets a partition of its own, with its own copy of every index
(its own HNSW graph, for chunks). Queries go through the parent, as before, so no
application code changes; the catalogue lint counts a partition as its parent's.

Only the tables listed here may be partitioned: large, not in the audit chain (whose
trigger stays on unpartitioned tables), and with every unique key starting with the
tenant, which Postgres requires of a table partitioned by it; `partition_table` checks the
keys again and refuses a table whose key does not. Both steps run as the
admin role, each in one transaction: `partition_table` once per table, `own_partition`
once per large tenant.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg
from psycopg import sql

PARTITIONABLE = ("tool_results",)  # more as their keys come to start with tenant_id
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def _check(table: str) -> None:
    if table not in PARTITIONABLE:
        raise ValueError(f"{table} may not be partitioned; allowed: {PARTITIONABLE}")


def is_partitioned(conn: psycopg.Connection[Any], table: str) -> bool:
    row = conn.execute(
        "SELECT c.relkind = 'p' FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relname = %s",
        (table,),
    ).fetchone()
    return bool(row and row[0])


def partition_table(
    conn: psycopg.Connection[Any], table: str, app_role: str, agent_role: str | None = None
) -> bool:
    """Turn a table into a parent partitioned by tenant, with a default partition holding
    every row; its grants, row-level security and policy are kept. False when it already is."""
    _check(table)
    if is_partitioned(conn, table):
        return False
    loose = conn.execute(
        "SELECT i.indexrelid::regclass::text FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid"
        " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id'"
        " WHERE c.relname = %s AND i.indisunique AND NOT a.attnum = ANY (i.indkey)",
        (table,),
    ).fetchall()
    if loose:
        raise ValueError(f"{table}: unique keys without tenant_id: {[r[0] for r in loose]}")
    old = f"{table}_unpartitioned"
    privileges = conn.execute(
        "SELECT grantee, string_agg(privilege_type, ', ') FROM information_schema.role_table_grants"
        " WHERE table_name = %s AND table_schema = 'public' GROUP BY grantee",
        (table,),
    ).fetchall()
    with conn.transaction():
        conn.execute(f"ALTER TABLE {table} RENAME TO {old}")
        conn.execute(
            f"CREATE TABLE {table} (LIKE {old} INCLUDING DEFAULTS INCLUDING CONSTRAINTS"
            " INCLUDING INDEXES) PARTITION BY LIST (tenant_id)"
        )
        conn.execute(f"CREATE TABLE {table}_default PARTITION OF {table} DEFAULT")
        conn.execute(f"INSERT INTO {table} SELECT * FROM {old}")
        conn.execute(f"DROP TABLE {old}")
        for grantee, granted in privileges:
            if grantee in (app_role, agent_role):
                conn.execute(f"GRANT {granted} ON {table} TO {grantee}")
        conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        conn.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        conn.execute(
            f"CREATE POLICY tenant_isolation ON {table}"
            " USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant())"
        )
    return True


def partition_name(table: str, tenant_id: str) -> str:
    safe = re.sub(r"[^a-z0-9]", "_", tenant_id.lower())
    name = f"{table}_t_{safe}"
    if not _IDENT.match(name):
        raise ValueError(f"{name!r} is not a usable partition name")
    return name


def own_partition(conn: psycopg.Connection[Any], table: str, tenant_id: str) -> str:
    """Move one tenant's rows into a partition of its own, with its own indexes: the
    default partition is detached, the tenant's rows moved, and both attached again, in
    one transaction."""
    _check(table)
    if not is_partitioned(conn, table):
        raise ValueError(f"{table} is not partitioned yet")
    name = partition_name(table, tenant_id)
    with conn.transaction():
        conn.execute(f"ALTER TABLE {table} DETACH PARTITION {table}_default")
        conn.execute(f"CREATE TABLE {name} (LIKE {table} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")
        conn.execute(
            f"INSERT INTO {name} SELECT * FROM {table}_default WHERE tenant_id = %s", (tenant_id,)
        )
        conn.execute(f"DELETE FROM {table}_default WHERE tenant_id = %s", (tenant_id,))
        conn.execute(
            sql.SQL("ALTER TABLE {} ATTACH PARTITION {} FOR VALUES IN ({})").format(
                sql.Identifier(table), sql.Identifier(name), sql.Literal(tenant_id)
            )
        )
        conn.execute(f"ALTER TABLE {table} ATTACH PARTITION {table}_default DEFAULT")
    return name


def layout(conn: psycopg.Connection[Any], table: str) -> dict[str, int]:
    """Each partition of a table and how many rows it holds (as the admin)."""
    rows = conn.execute(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
        " JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = %s ORDER BY 1",
        (table,),
    ).fetchall()
    out: dict[str, int] = {}
    for (name,) in rows:
        count = conn.execute(f"SELECT count(*) FROM {name}").fetchone()  # noqa: S608
        out[str(name)] = int(count[0]) if count else 0
    return out
