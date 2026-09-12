"""agent_threads: one row per graph run (spec.md Layer 8). Runners write here; graphs never do."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ccp.storage.scoped import ScopedDB

_COLUMNS = (
    "thread_id, tenant_id, graph, test_id, subject_key, status, interrupt, decision, verdict_id,"
    " error, trajectory, created_at, updated_at"
)


@dataclass(frozen=True)
class ThreadRow:
    thread_id: str
    tenant_id: str
    graph: str
    test_id: str | None
    subject_key: str | None
    status: str
    interrupt: dict[str, Any] | None
    decision: dict[str, Any] | None
    verdict_id: str | None
    error: str | None
    trajectory: list[str]
    created_at: datetime
    updated_at: datetime


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _row(r: tuple[Any, ...]) -> ThreadRow:
    return ThreadRow(
        thread_id=str(r[0]),
        tenant_id=str(r[1]),
        graph=str(r[2]),
        test_id=r[3],
        subject_key=r[4],
        status=str(r[5]),
        interrupt=_load(r[6]),
        decision=_load(r[7]),
        verdict_id=r[8],
        error=r[9],
        trajectory=list(_load(r[10]) or []),
        created_at=r[11],
        updated_at=r[12],
    )


def create(db: ScopedDB, thread_id: str, graph: str, test_id: str, subject_key: str) -> None:
    db.execute(
        "INSERT INTO agent_threads (thread_id, tenant_id, graph, test_id, subject_key, status)"
        " VALUES (%s, %s, %s, %s, %s, 'running')",
        (thread_id, db.tenant.tenant_id, graph, test_id, subject_key),
    )


def mark_interrupted(
    db: ScopedDB, thread_id: str, payload: dict[str, Any], trajectory: list[str]
) -> None:
    db.execute(
        "UPDATE agent_threads SET status = 'interrupted', interrupt = %s, trajectory = %s,"
        " updated_at = now() WHERE thread_id = %s",
        (json.dumps(payload, default=str), json.dumps(trajectory), thread_id),
    )


def mark_finished(
    db: ScopedDB,
    thread_id: str,
    verdict_id: str,
    trajectory: list[str],
    decision: dict[str, Any] | None = None,
) -> None:
    db.execute(
        "UPDATE agent_threads SET status = 'finished', verdict_id = %s, trajectory = %s,"
        " decision = COALESCE(%s, decision), updated_at = now() WHERE thread_id = %s",
        (
            verdict_id,
            json.dumps(trajectory),
            json.dumps(decision, default=str) if decision is not None else None,
            thread_id,
        ),
    )


def mark_failed(db: ScopedDB, thread_id: str, error: str, trajectory: list[str]) -> None:
    db.execute(
        "UPDATE agent_threads SET status = 'failed', error = %s, trajectory = %s,"
        " updated_at = now() WHERE thread_id = %s",
        (error[:2000], json.dumps(trajectory), thread_id),
    )


def get(db: ScopedDB, thread_id: str) -> ThreadRow | None:
    row = db.fetch_one(f"SELECT {_COLUMNS} FROM agent_threads WHERE thread_id = %s", (thread_id,))
    return _row(row) if row else None


def pending(db: ScopedDB) -> list[ThreadRow]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM agent_threads WHERE status = 'interrupted' ORDER BY created_at"
    )
    return [_row(r) for r in rows]


def all_threads(db: ScopedDB, limit: int = 50) -> list[ThreadRow]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM agent_threads ORDER BY created_at DESC LIMIT %s", (limit,)
    )
    return [_row(r) for r in rows]
