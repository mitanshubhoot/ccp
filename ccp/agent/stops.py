"""Stopping a run (spec.md A-135, report 6.10 "Interrupt and steer").

A person asks; the run stops itself. The request is a row, and the run's budget tracker asks
for it at every node boundary and tool call, where the wall clock already stops runs
(A-108), so Stop never depends on the agent noticing or agreeing. A stopped run ends
cancelled: it keeps its record (steps, trajectory, run log) and changes no status. Its job
ends without a retry, and a person may start it again.
"""

from __future__ import annotations

from collections.abc import Callable

from ccp.storage.scoped import ScopedDB


def request(db: ScopedDB, thread_id: str, requested_by: str, reason: str | None = None) -> bool:
    """Ask the run to stop; False when it was asked already."""
    with db.transaction() as cur:
        row = cur.execute(
            "INSERT INTO run_stops (tenant_id, thread_id, requested_by, reason)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING thread_id",
            (db.tenant.tenant_id, thread_id, requested_by, reason),
        ).fetchone()
    return row is not None


def requested(db: ScopedDB, thread_id: str) -> str | None:
    """Who asked this run to stop, if anyone has."""
    row = db.fetch_one("SELECT requested_by FROM run_stops WHERE thread_id = %s", (thread_id,))
    return str(row[0]) if row else None


def watcher(db: ScopedDB, thread_id: str) -> Callable[[], str | None]:
    return lambda: requested(db, thread_id)
