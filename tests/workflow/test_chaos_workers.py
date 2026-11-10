"""MT2 (A-111): three worker processes, an overlapping deploy, and hard kills mid-step.

Three evaluation workers and a state writer run as separate processes over a tenant's full
tier 1 run, each evaluation paused so a kill lands mid-step. A second set starts while the
first still runs (an overlapping deploy); then the first set is killed outright, holding
claims. The second set must finish everything: every record done once, one verdict per
evaluation key, one state row per verdict, nothing dead-lettered, and a killed worker's
records re-claimed after their leases ran out. Sampled throughout, the tenant's cap and the
one-evaluation-per-subject rule are never broken.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import pytest

from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db
SCRIPT = Path(__file__).resolve().parents[1] / "support" / "chaos_worker.py"
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
PAUSE_MS = 400
LEASE_SECONDS = 2


@pytest.fixture
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_chaos", ("acme",)) as (settings, dbs):
        yield settings, dbs["acme"]


class _Fleet:
    def __init__(self, settings: Settings, logs: Path) -> None:
        self.settings = settings
        self.logs = logs
        self.procs: list[subprocess.Popen[bytes]] = []
        self.files: list[IO[bytes]] = []

    def start(self, consumer: str, n: int = 1) -> list[subprocess.Popen[bytes]]:
        started = []
        for _ in range(n):
            log = (self.logs / f"{consumer}-{len(self.procs)}.log").open("wb")
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    self.settings.database_url,
                    consumer,
                    "acme",
                    str(PAUSE_MS),
                    str(LEASE_SECONDS),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            self.files.append(log)
            self.procs.append(proc)
            started.append(proc)
        return started

    def kill(self, procs: list[subprocess.Popen[bytes]]) -> None:
        for proc in procs:
            proc.kill()  # no clean shutdown: the worker dies holding whatever it claimed
        for proc in procs:
            proc.wait(30)

    def stop_all(self) -> None:
        self.kill([p for p in self.procs if p.poll() is None])
        for f in self.files:
            f.close()


def _count(acme: ScopedDB, sql: str) -> int:
    row = acme.fetch_one(sql)
    return int(row[0]) if row and row[0] is not None else 0


def test_workers_killed_mid_step_during_a_deploy_lose_and_duplicate_nothing(
    world: tuple[Settings, ScopedDB], tmp_path: Path
) -> None:
    settings, acme = world
    verdicts_before = _count(acme, "SELECT count(*) FROM verdicts")
    enqueued = evaluations.enqueue_all(acme, settings, now=NOW, tiers=(1,))
    assert enqueued >= 10
    cap = acme.tenant.settings.max_concurrency
    live, per_subject = [0], [0]

    def sample() -> None:
        live[0] = max(
            live[0],
            _count(
                acme,
                "SELECT count(*) FROM event_log WHERE topic = 'evaluations'"
                " AND status = 'claimed' AND lease_expires_at > now()",
            ),
        )
        per_subject[0] = max(
            per_subject[0],
            _count(
                acme,
                "SELECT max(n) FROM (SELECT count(*) AS n FROM event_log"
                " WHERE topic = 'evaluations' AND status = 'claimed'"
                " AND lease_expires_at > now() GROUP BY partition_key) AS held",
            ),
        )

    def done() -> int:
        return _count(
            acme, "SELECT count(*) FROM event_log WHERE topic = 'evaluations' AND status = 'done'"
        )

    def wait_for(predicate: Callable[[], bool], timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            assert time.monotonic() < deadline, f"timed out waiting for {what}; see {tmp_path}"
            sample()
            time.sleep(0.1)

    fleet = _Fleet(settings, tmp_path)
    try:
        old = fleet.start("evaluations", 3) + fleet.start("state")
        wait_for(lambda: done() >= 3, 120, "the first deploy to make progress")
        fleet.start("evaluations", 3)  # the new deploy comes up while the old one runs
        fleet.start("state")
        wait_for(lambda: done() >= 6, 120, "both deploys to run side by side")
        fleet.kill(old)  # the old deploy dies holding claims, mid-step
        wait_for(lambda: done() == enqueued, 180, "the new deploy to finish everything")
        wait_for(
            lambda: (
                _count(acme, "SELECT count(*) FROM compliance_state")
                == _count(acme, "SELECT count(*) FROM verdicts")
            ),
            120,
            "the state writer to catch up",
        )
    finally:
        fleet.stop_all()

    statuses = dict(
        acme.fetch_all(
            "SELECT status, count(*) FROM event_log WHERE topic = 'evaluations' GROUP BY status"
        )
    )
    assert statuses == {"done": enqueued}
    verdicts, keys = acme.fetch_one(
        "SELECT count(*), count(DISTINCT evaluation_key) FROM verdicts"
    ) or (0, 0)
    assert verdicts == keys == verdicts_before + enqueued  # none lost, none twice
    rows, distinct = acme.fetch_one(
        "SELECT count(*), count(DISTINCT verdict_id) FROM compliance_state"
    ) or (0, 0)
    assert rows == distinct == verdicts  # one state row per verdict
    assert evaluations.unannounced(acme) == 0
    assert _count(acme, "SELECT count(*) FROM dead_letter") == 0
    assert _count(acme, "SELECT count(*) FROM ordered_failures WHERE status = 'dead'") == 0
    # a killed worker's claims came back and were finished by the new deploy
    assert _count(acme, "SELECT max(attempts) FROM event_log WHERE topic = 'evaluations'") >= 2
    assert 1 <= live[0] <= cap
    assert per_subject[0] == 1
