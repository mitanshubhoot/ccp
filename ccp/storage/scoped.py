"""ScopedDB: the only query surface for tenant data.

Every transaction opened here sets ``app.tenant_id`` for its own duration, and every
row-level security policy reads that setting through ``app_tenant()``, which raises when
it is missing. Isolation is therefore enforced three times: by the ``TenantContext``
type, by this class, and by the database (spec.md section 2 and Layer 5).

    ScopedDB(pool, tenant)
      └── transaction()
            ├── BEGIN
            ├── SELECT set_config('app.tenant_id', <tenant>, true)   -- local to this transaction
            ├── ... caller's statements; RLS filters and checks every row ...
            └── COMMIT (or ROLLBACK on error; the setting dies with the transaction)

There is no method that accepts a tenant id argument, on purpose.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from ccp.tenancy import TenantContext

Params = Sequence[Any] | Mapping[str, Any] | None


class ScopedDB:
    def __init__(self, pool: ConnectionPool, tenant: TenantContext) -> None:
        self._pool = pool
        self.tenant = tenant

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Cursor[Any]]:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", (self.tenant.tenant_id,))
            yield cur

    def fetch_all(self, query: str, params: Params = None) -> list[tuple[Any, ...]]:
        with self.transaction() as cur:
            cur.execute(query, params)
            return cur.fetchall()

    def fetch_one(self, query: str, params: Params = None) -> tuple[Any, ...] | None:
        with self.transaction() as cur:
            cur.execute(query, params)
            return cur.fetchone()

    def execute(self, query: str, params: Params = None) -> int:
        with self.transaction() as cur:
            cur.execute(query, params)
            return cur.rowcount
