"""Production quality signals per test (spec.md A-143).

A model's refusal ends its evaluation (a terminal error, dead-lettered); it is also recorded
here, with the test and the bundle, so the refusal rate of each test can be watched and
gated (A-144). The rate is refusals over attempts (verdicts plus refusals) in a window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ulid import ULID

from ccp.config import Settings
from ccp.llm import release
from ccp.storage.scoped import ScopedDB


@dataclass(frozen=True)
class Rate:
    test_id: str
    refusals: int
    attempts: int

    @property
    def rate(self) -> float:
        return self.refusals / self.attempts if self.attempts else 0.0


def record_refusal(db: ScopedDB, test_id: str, settings: Settings, detail: str) -> None:
    db.execute(
        "INSERT INTO model_refusals (tenant_id, refusal_id, test_id, bundle_id, detail)"
        " VALUES (%s, %s, %s, %s, %s)",
        (db.tenant.tenant_id, str(ULID()), test_id, release.bundle_id(settings), detail[:500]),
    )


def refusal_rates(db: ScopedDB, since: datetime) -> list[Rate]:
    refused = dict(
        db.fetch_all(
            "SELECT test_id, count(*) FROM model_refusals WHERE recorded_at >= %s GROUP BY 1",
            (since,),
        )
    )
    judged = dict(
        db.fetch_all(
            "SELECT test_id, count(*) FROM verdicts WHERE tier > 1 AND created_at >= %s GROUP BY 1",
            (since,),
        )
    )
    tests = sorted(set(refused) | set(judged))
    return [
        Rate(t, int(refused.get(t, 0)), int(refused.get(t, 0)) + int(judged.get(t, 0)))
        for t in tests
    ]
