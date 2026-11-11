"""A fix is done only when a later read sees it (spec.md A-160).

A fourth consumer of the verdicts topic. For each new verdict it looks at the suggestions
for the same test and subject that were made before the evidence it read was observed:

    the verdict passes, or it fails without naming the resource   -> confirmed
    the verdict fails naming the resource, within 30 days of a
    confirmation                                                    -> reopened

What a person records (accepted, dismissed) never confirms anything: only a verdict whose
read came after the suggestion does. A needs_review verdict decides nothing.

Also the numbers LT1 is judged by: how long from the failing verdict to the read that
confirmed its fix, and how many confirmed fixes failed again within 30 days.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from ulid import ULID

from ccp.evaluators import verdict as verdicts
from ccp.evaluators.verdict import Verdict
from ccp.ingest.consumers import OrderedConsumer
from ccp.ingest.event_log import LogRecord
from ccp.ingest.worker import register
from ccp.lookups.store import failing_of
from ccp.remediation.suggestions import LIVE, PEOPLE, Status, get, record_event
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus

REOPEN_WINDOW = timedelta(days=30)


def _event(
    cur: psycopg.Cursor[Any],
    tenant_id: str,
    suggestion_id: str,
    status: Status,
    verdict_id: str,
    note: str,
) -> None:
    cur.execute(
        "INSERT INTO fix_suggestion_events (tenant_id, event_id, suggestion_id, status, actor,"
        " note, verdict_id) VALUES (%s, %s, %s, %s, 'ccp', %s, %s)",
        (tenant_id, str(ULID()), suggestion_id, status, note, verdict_id),
    )


def _still_failing(verdict: Verdict, resource: str) -> bool:
    if verdict.status != VerdictStatus.failed:
        return False
    named = {f["name"] for f in failing_of(verdict)}
    return not named or resource in named  # a failure that names nothing covers everything


def settle(cur: psycopg.Cursor[Any], verdict: Verdict) -> dict[str, int]:
    """Confirm or reopen the suggestions this verdict's read decides."""
    out = {"confirmed": 0, "reopened": 0}
    if verdict.status not in (VerdictStatus.passed, VerdictStatus.failed):
        return out
    rows = cur.execute(
        "SELECT s.suggestion_id, s.resource, s.status, s.status_at FROM fix_suggestion_status s"
        " JOIN verdicts v ON v.tenant_id = s.tenant_id AND v.verdict_id = s.verdict_id"
        " WHERE s.test_id = %s AND v.subject_key = %s AND s.verdict_id <> %s"
        " AND s.created_at < %s",
        (verdict.test_id, verdict.subject_key, verdict.verdict_id, verdict.trigger_observed_at),
    ).fetchall()
    for suggestion_id, resource, status, status_at in rows:
        failing = _still_failing(verdict, str(resource))
        if status in LIVE and not failing:
            _event(
                cur,
                verdict.tenant_id,
                suggestion_id,
                "confirmed",
                verdict.verdict_id,
                f"{verdict.test_id} read at {verdict.trigger_observed_at.isoformat()}"
                " sees it fixed",
            )
            out["confirmed"] += 1
        elif (
            status == "confirmed"
            and failing
            and verdict.trigger_observed_at - status_at <= REOPEN_WINDOW
            and verdict.trigger_observed_at > status_at
        ):
            _event(
                cur,
                verdict.tenant_id,
                suggestion_id,
                "reopened",
                verdict.verdict_id,
                "failed again within 30 days of its confirmation",
            )
            out["reopened"] += 1
    return out


class FixConfirmations(OrderedConsumer):
    """Confirms a suggested fix only from a later verdict's read; never from a person."""

    name = "fix-confirmations"
    topic = "verdicts"

    def __init__(self, db: ScopedDB, settings: Any = None) -> None:
        super().__init__(db)
        self.confirmed = 0
        self.reopened = 0

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        verdict = verdicts.load(self._db, record.ref)
        if verdict is None:
            return
        done = settle(cur, verdict)
        self.confirmed += done["confirmed"]
        self.reopened += done["reopened"]


register("fix-confirmations")(lambda db, settings: FixConfirmations(db, settings))


def drain(db: ScopedDB) -> FixConfirmations:
    consumer = FixConfirmations(db)
    while consumer.run_once():
        pass
    return consumer


def decide(db: ScopedDB, suggestion_id: str, status: Status, by: str, note: str = "") -> None:
    """What a person records about a suggestion: accepted or dismissed (a dismissal says
    why). Neither confirms it."""
    if status not in PEOPLE:
        raise ValueError(f"a person records {PEOPLE}, not {status!r}")
    found = get(db, suggestion_id)
    if found is None:
        raise ValueError(f"no suggestion {suggestion_id!r}")
    if found.status not in LIVE:
        raise ValueError(f"suggestion {suggestion_id} is already {found.status}")
    if status == "dismissed" and not note.strip():
        raise ValueError("a dismissal says why")
    record_event(db, suggestion_id, status, by, note)


@dataclass(frozen=True)
class Metrics:
    by_status: dict[str, int]
    median_hours_to_confirmed: float | None
    confirmed: int
    reopened: int

    @property
    def reopen_rate(self) -> float | None:
        return self.reopened / self.confirmed if self.confirmed else None


def metrics(db: ScopedDB, since: datetime | None = None) -> Metrics:
    """LT1's numbers: suggestions by status, the median time from the failing verdict to
    the read that confirmed its fix, and the share of confirmed fixes that failed again."""
    counts = db.fetch_all(
        "SELECT status, count(*) FROM fix_suggestion_status"
        " WHERE (%s::timestamptz IS NULL OR created_at >= %s) GROUP BY status",
        (since, since),
    )
    hours = db.fetch_all(
        "SELECT EXTRACT(EPOCH FROM (c.valid_from - f.valid_from)) / 3600"
        " FROM fix_suggestion_events e"
        " JOIN fix_suggestions s ON s.tenant_id = e.tenant_id AND s.suggestion_id = e.suggestion_id"
        " JOIN verdicts f ON f.tenant_id = s.tenant_id AND f.verdict_id = s.verdict_id"
        " JOIN verdicts c ON c.tenant_id = e.tenant_id AND c.verdict_id = e.verdict_id"
        " WHERE e.status = 'confirmed' AND (%s::timestamptz IS NULL OR s.created_at >= %s)",
        (since, since),
    )
    reopened = db.fetch_one(
        "SELECT count(DISTINCT suggestion_id) FROM fix_suggestion_events WHERE status = 'reopened'"
    )
    values = [float(h[0]) for h in hours if h[0] is not None]
    by_status = {str(r[0]): int(r[1]) for r in counts}
    return Metrics(
        by_status,
        statistics.median(values) if values else None,
        len(values),
        int(reopened[0]) if reopened else 0,
    )
