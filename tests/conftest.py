"""Shared fixtures.

``db_settings`` creates a throwaway database per pytest session from the admin URL,
migrates it, and drops it at exit. Tests marked ``db`` skip cleanly when Postgres is not
reachable, so the no-key, no-Docker path stays green (spec.md Phase 0 and A-27).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool

from ccp.config import Settings
from ccp.observability import steps
from ccp.storage.db import app_pool, migrate

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def base_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    data_dir = tmp_path_factory.mktemp("ccp-data")
    settings = Settings(ccp_data_dir=data_dir)
    steps.configure(settings)
    return settings


def _with_dbname(url: str, dbname: str) -> str:
    info = conninfo_to_dict(url)
    info["dbname"] = dbname
    return make_conninfo(**info)


@pytest.fixture(scope="session")
def db_settings(base_settings: Settings) -> Iterator[Settings]:
    maintenance_url = _with_dbname(base_settings.database_admin_url, "postgres")
    try:
        admin = psycopg.connect(maintenance_url, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable for db tests: {exc}")
    name = f"ccp_test_{uuid.uuid4().hex[:8]}"
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings = base_settings.model_copy(
        update={
            "database_url": _with_dbname(base_settings.database_url, name),
            "database_admin_url": _with_dbname(base_settings.database_admin_url, name),
        }
    )
    migrate(settings)
    try:
        yield settings
    finally:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture(scope="session")
def pool(db_settings: Settings) -> Iterator[ConnectionPool]:
    p = app_pool(db_settings, max_size=4)
    try:
        yield p
    finally:
        p.close()
