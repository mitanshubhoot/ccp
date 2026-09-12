"""Two workers on a parallel topic; one is killed mid-handle. Exactly one verdict-equivalent lands.

Worker A claims the record, writes a started marker, and sleeps. The test kills it. After
the visibility timeout, worker B re-claims the same record and completes it. The effects
file must have exactly one line and the row must be done with attempts == 2.

Runs in its own scratch database so no other test's queue state can be claimed.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ccp.config import Settings
from ccp.ingest import event_log
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

pytestmark = pytest.mark.db
SCRIPT = Path(__file__).resolve().parents[1] / "support" / "slow_worker.py"


@pytest.fixture
def own_db(base_settings: Settings) -> Iterator[Settings]:
    try:
        with scratch_database(base_settings, prefix="ccp_kill") as settings:
            yield settings
    except psycopg.OperationalError as exc:  # pragma: no cover
        pytest.skip(f"Postgres not reachable: {exc}")


def _wait_for(predicate, timeout: float, what: str) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def test_killed_worker_is_reclaimed_and_the_record_completes_once(
    own_db: Settings, tmp_path: Path
) -> None:
    pool = app_pool(own_db, max_size=2)
    try:
        acme = ScopedDB(pool, TenantContext.for_id(own_db, "acme"))
        with acme.transaction() as cur:
            offset = event_log.append(
                cur,
                tenant_id="acme",
                topic="evaluations",
                partition_key="kill-test",
                source_ts=datetime(2026, 8, 1, tzinfo=UTC),
                ref="kill-test",
            )
        visibility = 2
        args = [sys.executable, str(SCRIPT), own_db.database_url, "acme", str(tmp_path)]

        worker_a = subprocess.Popen([*args, "60", str(visibility)])
        try:
            _wait_for(lambda: any(tmp_path.glob("started-*")), 30, "worker A to claim")
            with acme.transaction() as cur:
                assert event_log.status_of(cur, offset)[:2] == ("claimed", 1)
        finally:
            worker_a.kill()
            worker_a.wait(timeout=30)
        assert not (tmp_path / "effects").exists()

        time.sleep(visibility + 0.5)  # let the claim expire
        worker_b = subprocess.run(
            [*args, "0", str(visibility)], capture_output=True, text=True, timeout=60
        )
        assert worker_b.returncode == 0, worker_b.stderr

        effects = (tmp_path / "effects").read_text(encoding="utf-8").splitlines()
        assert len(effects) == 1 and effects[0].startswith(f"done:{offset}:")
        with acme.transaction() as cur:
            assert event_log.status_of(cur, offset) == ("done", 2, None)
    finally:
        pool.close()
