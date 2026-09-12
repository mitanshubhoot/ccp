"""Test helper: bring a scratch database to the state evaluators expect.

ingest (fixtures) -> normalize -> identity resolve, for the requested tenants.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ccp.config import Settings
from ccp.identity import resolver
from ccp.ingest.landing import ingest_tenant
from ccp.normalize.consumer import normalize_all
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext


def prepare(settings: Settings, pool: ConnectionPool, tenant_id: str) -> ScopedDB:
    db = ScopedDB(pool, TenantContext.for_id(settings, tenant_id))
    ingest_tenant(settings, db, db.tenant)
    normalize_all(db, settings)
    resolver.resolve_tenant(db)
    return db


@contextmanager
def prepared_tenants(
    base_settings: Settings, prefix: str, tenants: Sequence[str] = ("acme", "globex")
) -> Iterator[tuple[Settings, dict[str, ScopedDB]]]:
    """A module-scoped scratch database with the tenants ingested, normalized, and resolved."""
    try:
        with scratch_database(base_settings, prefix=prefix) as settings:
            pool = app_pool(settings, max_size=4)
            try:
                yield settings, {t: prepare(settings, pool, t) for t in tenants}
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")
