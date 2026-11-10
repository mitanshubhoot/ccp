"""Sync runs: what each connector pull saw, scope by scope (spec.md A-118).

A pull's run is recorded whatever happened to it: complete, partial, or failed with the
error's class. One connector's failure ends its own run only; the next connector still
runs. Only a scope a run marked complete may turn an absence into a fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ulid import ULID

from ccp.connectors.base import RunStatus, ScopeOutcome
from ccp.storage.scoped import ScopedDB


@dataclass(frozen=True)
class SyncRun:
    run_id: str
    connector: str
    source: str
    status: RunStatus
    scopes: dict[str, ScopeOutcome]
    landed: int
    duplicates: int
    started_at: datetime
    finished_at: datetime
    error: str | None = None
    error_kind: str | None = None  # auth, not_visible or fail, when the run failed (A-120)

    @property
    def error_class(self) -> str | None:
        return self.error.split(":", 1)[0] if self.error else None


def new_run_id() -> str:
    return str(ULID())


def record(db: ScopedDB, run: SyncRun, connection_id: str | None = None) -> None:
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO sync_runs (run_id, tenant_id, connection_id, connector, source, status,"
            " started_at, finished_at, events_landed, duplicates, error_class, error,"
            " error_kind) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run.run_id,
                db.tenant.tenant_id,
                connection_id,
                run.connector,
                run.source,
                run.status,
                run.started_at,
                run.finished_at,
                run.landed,
                run.duplicates,
                run.error_class,
                (run.error or "")[:2000] or None,
                run.error_kind,
            ),
        )
        for scope, outcome in run.scopes.items():
            cur.execute(
                "INSERT INTO sync_run_scopes (tenant_id, run_id, scope, status, items, detail)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    db.tenant.tenant_id,
                    run.run_id,
                    scope,
                    outcome.status,
                    outcome.items,
                    (outcome.detail or "")[:2000] or None,
                ),
            )


def recent(db: ScopedDB, limit: int = 20) -> list[dict[str, Any]]:
    """The tenant's latest runs, newest first, each with its scopes."""
    runs = db.fetch_all(
        "SELECT run_id, connector, source, status, finished_at, events_landed, duplicates,"
        " error, error_kind FROM sync_runs ORDER BY finished_at DESC, run_id DESC LIMIT %s",
        (limit,),
    )
    out: list[dict[str, Any]] = []
    for run_id, connector, source, status, finished, landed, duplicates, error, kind in runs:
        scopes = db.fetch_all(
            "SELECT scope, status, items, detail FROM sync_run_scopes WHERE run_id = %s"
            " ORDER BY scope",
            (run_id,),
        )
        out.append(
            {
                "run_id": run_id,
                "connector": connector,
                "source": source,
                "status": status,
                "finished_at": finished,
                "landed": landed,
                "duplicates": duplicates,
                "error": error,
                "error_kind": kind,
                "scopes": {s: {"status": st, "items": n, "detail": d} for s, st, n, d in scopes},
            }
        )
    return out


def last_complete(db: ScopedDB, connector: str, scope: str) -> datetime | None:
    """When the scope was last seen in full: the newest run that marked it complete."""
    row = db.fetch_one(
        "SELECT max(r.finished_at) FROM sync_runs r JOIN sync_run_scopes s"
        " ON s.tenant_id = r.tenant_id AND s.run_id = r.run_id"
        " WHERE r.connector = %s AND s.scope = %s AND s.status = 'complete'",
        (connector, scope),
    )
    return row[0] if row and row[0] is not None else None
