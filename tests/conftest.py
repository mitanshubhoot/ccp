"""Shared fixtures.

``db_settings`` creates a throwaway database per pytest session through the same
``scratch_database`` helper the eval runner uses, migrates it, and drops it at exit.
Tests marked ``db`` skip cleanly when Postgres is not reachable, so the no-key,
no-Docker path stays green (spec.md Phase 0 and A-27).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ccp.config import Settings
from ccp.observability import steps
from ccp.storage.db import app_pool, scratch_database

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def base_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    data_dir = tmp_path_factory.mktemp("ccp-data")
    settings = Settings(ccp_data_dir=data_dir)
    steps.configure(settings, sink=None)  # tests never emit traces, whatever the env holds
    return settings


@pytest.fixture(scope="session")
def db_settings(base_settings: Settings) -> Iterator[Settings]:
    try:
        with scratch_database(base_settings, prefix="ccp_test") as settings:
            yield settings
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable for db tests: {exc}")


@pytest.fixture(scope="session")
def pool(db_settings: Settings) -> Iterator[ConnectionPool]:
    p = app_pool(db_settings, max_size=4)
    try:
        yield p
    finally:
        p.close()
