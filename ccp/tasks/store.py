"""Tasks and items as typed rows (spec.md A-150, report 6.7 "Progress records").

    tasks        a long task: its kind, scope, caps and spend, open | done | parked | cancelled
    task_items   one row per item a code planner wrote: check (settled by code from the
                 rules' verdicts) or read (a model run over evidence); open | running |
                 waiting | final | needs_review | cancelled
    task_events  every change, run, cap and alert, insert-only and on the audit chain

This module is the only writer, and the task runner its only caller for statuses: no model
writes a row here, and nothing marks a task done but the runner finding every item final.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from ulid import ULID

from ccp.storage.scoped import ScopedDB

TaskKind = Literal["audit_prep", "questionnaire"]
TaskStatus = Literal["open", "done", "parked", "cancelled"]
ItemKind = Literal["check", "read"]
ItemStatus = Literal["open", "running", "waiting", "final", "needs_review", "cancelled"]
SETTLED: frozenset[str] = frozenset({"final", "cancelled"})

_TASK_COLUMNS = (
    "task_id, kind, title, scope, status, parked_reason, max_usd, forecast_usd, deadline,"
    " max_runs_per_item, spent_usd, passes, stalled_passes, alerted, bundle_id, created_by,"
    " created_at, updated_at"
)
_ITEM_COLUMNS = (
    "task_id, item_id, kind, question, inputs, status, outcome, attempts, evidence_ids,"
    " open_question, last_run_id, result, created_at, updated_at"
)


@dataclass(frozen=True)
class Task:
    task_id: str
    kind: str
    title: str
    scope: dict[str, Any]
    status: str
    parked_reason: str | None
    max_usd: float
    forecast_usd: float
    deadline: datetime
    max_runs_per_item: int
    spent_usd: float
    passes: int
    stalled_passes: int
    alerted: bool
    bundle_id: str
    created_by: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, r: tuple[Any, ...]) -> Task:
        return cls(
            task_id=r[0],
            kind=r[1],
            title=r[2],
            scope=_json(r[3]) or {},
            status=r[4],
            parked_reason=r[5],
            max_usd=float(r[6]),
            forecast_usd=float(r[7]),
            deadline=r[8],
            max_runs_per_item=int(r[9]),
            spent_usd=float(r[10]),
            passes=int(r[11]),
            stalled_passes=int(r[12]),
            alerted=bool(r[13]),
            bundle_id=r[14],
            created_by=r[15],
            created_at=r[16],
            updated_at=r[17],
        )


@dataclass(frozen=True)
class Item:
    task_id: str
    item_id: str
    kind: str
    question: str
    inputs: dict[str, Any]
    status: str
    outcome: str | None
    attempts: int
    evidence_ids: tuple[str, ...]
    open_question: str | None
    last_run_id: str | None
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    @property
    def settled(self) -> bool:
        return self.status in SETTLED

    @classmethod
    def from_row(cls, r: tuple[Any, ...]) -> Item:
        return cls(
            task_id=r[0],
            item_id=r[1],
            kind=r[2],
            question=r[3],
            inputs=_json(r[4]) or {},
            status=r[5],
            outcome=r[6],
            attempts=int(r[7]),
            evidence_ids=tuple(str(e) for e in (_json(r[8]) or [])),
            open_question=r[9],
            last_run_id=r[10],
            result=_json(r[11]),
            created_at=r[12],
            updated_at=r[13],
        )


@dataclass(frozen=True)
class NewItem:
    """What a planner writes: the item's key within its task, its kind, the question it
    answers, and its typed inputs (facts and handles, never a transcript)."""

    item_id: str
    kind: ItemKind
    question: str
    inputs: dict[str, Any] = field(default_factory=dict)


def _json(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else value


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def event(
    db: ScopedDB,
    task_id: str,
    kind: str,
    detail: dict[str, Any] | None = None,
    *,
    item_id: str | None = None,
    run_id: str | None = None,
    usd: float | None = None,
) -> str:
    event_id = str(ULID())
    db.execute(
        "INSERT INTO task_events (tenant_id, event_id, task_id, item_id, kind, detail, run_id,"
        " usd) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            event_id,
            task_id,
            item_id,
            kind,
            _dumps(detail or {}),
            run_id,
            Decimal(str(round(usd, 6))) if usd is not None else None,
        ),
    )
    return event_id


def create(
    db: ScopedDB,
    *,
    kind: TaskKind,
    title: str,
    scope: dict[str, Any],
    items: list[NewItem],
    max_usd: float,
    forecast_usd: float,
    deadline: datetime,
    max_runs_per_item: int,
    bundle_id: str,
    created_by: str,
) -> Task:
    ids = [i.item_id for i in items]
    if len(ids) != len(set(ids)):
        raise ValueError("a planner wrote two items with the same id")
    task_id = str(ULID())
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO tasks (tenant_id, task_id, kind, title, scope, max_usd, forecast_usd,"
            " deadline, max_runs_per_item, bundle_id, created_by)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                db.tenant.tenant_id,
                task_id,
                kind,
                title,
                _dumps(scope),
                Decimal(str(round(max_usd, 6))),
                Decimal(str(round(forecast_usd, 6))),
                deadline,
                max_runs_per_item,
                bundle_id,
                created_by,
            ),
        )
        for item in items:
            _insert_item(cur, db.tenant.tenant_id, task_id, item)
        cur.execute(
            "INSERT INTO task_events (tenant_id, event_id, task_id, kind, detail)"
            " VALUES (%s, %s, %s, 'planned', %s)",
            (
                db.tenant.tenant_id,
                str(ULID()),
                task_id,
                _dumps(
                    {
                        "kind": kind,
                        "items": len(items),
                        "read_items": sum(1 for i in items if i.kind == "read"),
                        "forecast_usd": round(forecast_usd, 6),
                        "max_usd": round(max_usd, 6),
                        "deadline": deadline.isoformat(),
                        "bundle_id": bundle_id,
                        "created_by": created_by,
                    }
                ),
            ),
        )
    found = get(db, task_id)
    assert found is not None
    return found


def _insert_item(cur: Any, tenant_id: str, task_id: str, item: NewItem) -> None:
    cur.execute(
        "INSERT INTO task_items (tenant_id, task_id, item_id, kind, question, inputs)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (tenant_id, task_id, item.item_id, item.kind, item.question, _dumps(item.inputs)),
    )


def add_items(db: ScopedDB, task_id: str, items: list[NewItem], why: str) -> list[str]:
    """Items code adds while the task runs (a family's gap read once its checks settle).
    An id already present is skipped, so adding twice adds once."""
    present = {i.item_id for i in items_of(db, task_id)}
    added: list[str] = []
    with db.transaction() as cur:
        for item in items:
            if item.item_id in present or item.item_id in added:
                continue
            _insert_item(cur, db.tenant.tenant_id, task_id, item)
            added.append(item.item_id)
        if added:
            cur.execute(
                "INSERT INTO task_events (tenant_id, event_id, task_id, kind, detail)"
                " VALUES (%s, %s, %s, 'items_added', %s)",
                (db.tenant.tenant_id, str(ULID()), task_id, _dumps({"items": added, "why": why})),
            )
    return added


def get(db: ScopedDB, task_id: str) -> Task | None:
    row = db.fetch_one(f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_id = %s", (task_id,))  # noqa: S608
    return Task.from_row(row) if row else None


def list_tasks(db: ScopedDB, status: str | None = None) -> list[Task]:
    rows = db.fetch_all(
        f"SELECT {_TASK_COLUMNS} FROM tasks"  # noqa: S608
        " WHERE (%s::text IS NULL OR status = %s) ORDER BY created_at DESC",
        (status, status),
    )
    return [Task.from_row(r) for r in rows]


def items_of(db: ScopedDB, task_id: str) -> list[Item]:
    rows = db.fetch_all(
        f"SELECT {_ITEM_COLUMNS} FROM task_items WHERE task_id = %s ORDER BY item_id",  # noqa: S608
        (task_id,),
    )
    return [Item.from_row(r) for r in rows]


def item(db: ScopedDB, task_id: str, item_id: str) -> Item | None:
    row = db.fetch_one(
        f"SELECT {_ITEM_COLUMNS} FROM task_items WHERE task_id = %s AND item_id = %s",  # noqa: S608
        (task_id, item_id),
    )
    return Item.from_row(row) if row else None


def events(db: ScopedDB, task_id: str) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        "SELECT event_id, item_id, kind, detail, run_id, usd, recorded_at FROM task_events"
        " WHERE task_id = %s ORDER BY recorded_at, event_id",
        (task_id,),
    )
    return [
        {
            "event_id": r[0],
            "item_id": r[1],
            "kind": r[2],
            "detail": _json(r[3]) or {},
            "run_id": r[4],
            "usd": float(r[5]) if r[5] is not None else None,
            "recorded_at": r[6],
        }
        for r in rows
    ]


_UNSET: Any = object()


def set_item(
    db: ScopedDB,
    task_id: str,
    item_id: str,
    *,
    status: ItemStatus,
    why: str,
    outcome: str | None = _UNSET,
    result: dict[str, Any] | None = _UNSET,
    evidence_ids: list[str] | tuple[str, ...] | None = _UNSET,
    open_question: str | None = _UNSET,
    last_run_id: str | None = _UNSET,
    attempts: int | None = _UNSET,
    run_id: str | None = None,
    usd: float | None = None,
) -> None:
    """Change one item and record why, in one transaction. Fields left unset keep their value."""
    sets = ["status = %s", "updated_at = clock_timestamp()"]
    params: list[Any] = [status]
    values: tuple[tuple[str, Any], ...] = (
        ("outcome", outcome),
        ("result", result),
        ("evidence_ids", evidence_ids),
        ("open_question", open_question),
        ("last_run_id", last_run_id),
        ("attempts", attempts),
    )
    for column, value in values:
        if value is _UNSET:
            continue
        sets.append(f"{column} = %s")
        if column == "evidence_ids":
            params.append(_dumps(list(value or ())))
        elif column == "result":
            params.append(_dumps(value))
        else:
            params.append(value)
    detail: dict[str, Any] = {"status": status, "why": why}
    if outcome is not _UNSET:
        detail["outcome"] = outcome
    with db.transaction() as cur:
        cur.execute(
            f"UPDATE task_items SET {', '.join(sets)}"  # noqa: S608
            " WHERE task_id = %s AND item_id = %s",
            (*params, task_id, item_id),
        )
        cur.execute(
            "INSERT INTO task_events (tenant_id, event_id, task_id, item_id, kind, detail,"
            " run_id, usd) VALUES (%s, %s, %s, %s, 'item_status', %s, %s, %s)",
            (
                db.tenant.tenant_id,
                str(ULID()),
                task_id,
                item_id,
                _dumps(detail),
                run_id,
                Decimal(str(round(usd, 6))) if usd is not None else None,
            ),
        )


def set_task(
    db: ScopedDB,
    task_id: str,
    *,
    status: TaskStatus | None = None,
    why: str,
    parked_reason: str | None = _UNSET,
    passes: int | None = None,
    stalled_passes: int | None = None,
    alerted: bool | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    sets = ["updated_at = clock_timestamp()"]
    params: list[Any] = []
    for column, value in (
        ("status", status),
        ("passes", passes),
        ("stalled_passes", stalled_passes),
        ("alerted", alerted),
    ):
        if value is not None:
            sets.append(f"{column} = %s")
            params.append(value)
    if parked_reason is not _UNSET:
        sets.append("parked_reason = %s")
        params.append(parked_reason)
    with db.transaction() as cur:
        cur.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = %s",  # noqa: S608
            (*params, task_id),
        )
        cur.execute(
            "INSERT INTO task_events (tenant_id, event_id, task_id, kind, detail)"
            " VALUES (%s, %s, %s, %s, %s)",
            (
                db.tenant.tenant_id,
                str(ULID()),
                task_id,
                why,
                _dumps({"status": status, **(detail or {})}),
            ),
        )


def charge(db: ScopedDB, task_id: str, usd: float) -> float:
    """Add a run's spend to its task; returns the new total."""
    row = db.fetch_one(
        "UPDATE tasks SET spent_usd = spent_usd + %s, updated_at = clock_timestamp()"
        " WHERE task_id = %s RETURNING spent_usd",
        (Decimal(str(round(usd, 6))), task_id),
    )
    return float(row[0]) if row else 0.0


def run_costs(db: ScopedDB, kind: str, limit: int = 200) -> list[float]:
    """The dollars of the last read-item runs of this task kind, for the forecast."""
    rows = db.fetch_all(
        "SELECT e.usd FROM task_events e JOIN tasks t ON t.task_id = e.task_id"
        " WHERE t.kind = %s AND e.kind = 'run_finished' AND e.usd IS NOT NULL"
        " ORDER BY e.recorded_at DESC LIMIT %s",
        (kind, limit),
    )
    return [float(r[0]) for r in rows]
