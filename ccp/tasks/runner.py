"""The task runner (spec.md A-150, report 6.7 "Multi-day tasks as resumable runs" and "Drift,
loops and premature completion"): the only code that changes a task or an item's status.

    plan     a code planner writes the items; the forecast is the expected read runs times
             the p95 cost of one (measured once 20 runs exist, a setting until then); the
             dollar cap defaults to three runs per item at that cost; the deadline to 14 days
    advance  one pass: caps first (deadline, dollars), then every open check item settled by
             code from the verdicts, follow-up items added by code, read items handed to the
             reader (the fan-out launcher, A-151), then done or stalled:
               done      every item final or cancelled, decided here and nowhere else
               stalled   a pass that attempted work and changed nothing; the third in a row
                         parks the task for a person
             a spend past 1.5 times the forecast records one cost alert; a spend at the cap,
             or the deadline passed, parks the task
    resolve  a person settles an item the runner held (needs_review, waiting), recorded
    resume   a person reopens a parked task, optionally with a new deadline or cap

A run that ends anything but cleanly never blocks the rest: its item alone is held.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ccp.config import Settings
from ccp.evaluators import jobs
from ccp.knowledge import graph as knowledge
from ccp.llm import release
from ccp.storage.scoped import ScopedDB
from ccp.tasks import store
from ccp.tasks.planners import Facts, planner_for
from ccp.tasks.store import Item, Task

MEASURED_AFTER = 20  # read runs of a kind before their p95 replaces the configured cost


@dataclass
class ReadOutcome:
    """What the reader did with the open read items this pass (A-151)."""

    ran: int = 0
    changed: int = 0
    usd: float = 0.0
    events: list[str] = field(default_factory=list)


Reader = Callable[[ScopedDB, Settings, Task, list[Item]], ReadOutcome]


@dataclass
class PassSummary:
    task_id: str
    status: str
    checked: int = 0
    changed: int = 0
    added: list[str] = field(default_factory=list)
    reads_waiting: int = 0
    ran: int = 0
    usd: float = 0.0
    parked_reason: str | None = None
    events: list[str] = field(default_factory=list)


def item_cost(db: ScopedDB, settings: Settings, kind: str) -> float:
    """The p95 dollars of one read run of this kind, or the configured cost until measured."""
    costs = sorted(store.run_costs(db, kind))
    if len(costs) < MEASURED_AFTER:
        return settings.ccp_task_item_usd
    return costs[max(0, math.ceil(0.95 * len(costs)) - 1)]


def plan(
    db: ScopedDB,
    settings: Settings,
    kind: str,
    title: str,
    scope: dict[str, Any],
    *,
    created_by: str,
    deadline: datetime | None = None,
    max_usd: float | None = None,
    now: datetime | None = None,
) -> Task:
    planner = planner_for(kind)
    items = planner.plan(knowledge.load(), scope)
    forecast = planner.expected_reads(items) * item_cost(db, settings, kind)
    runs = settings.ccp_task_max_runs_per_item
    moment = now or datetime.now(UTC)
    return store.create(
        db,
        kind=kind,  # type: ignore[arg-type]  # planner_for refused any other kind
        title=title,
        scope=scope,
        items=items,
        max_usd=max_usd if max_usd is not None else forecast * runs,
        forecast_usd=forecast,
        deadline=deadline or moment + timedelta(days=settings.ccp_task_deadline_days),
        max_runs_per_item=runs,
        bundle_id=release.bundle_id(settings),
        created_by=created_by,
    )


def _park(db: ScopedDB, task: Task, reason: str, summary: PassSummary, **detail: Any) -> None:
    store.set_task(
        db,
        task.task_id,
        status="parked",
        why="parked",
        parked_reason=reason,
        detail={"reason": reason, **detail},
    )
    summary.status = "parked"
    summary.parked_reason = reason
    summary.events.append(f"task_parked:{reason}")


def _alert(db: ScopedDB, settings: Settings, task_id: str, summary: PassSummary) -> None:
    task = store.get(db, task_id)
    if task is None or task.alerted or task.forecast_usd <= 0:
        return
    if task.spent_usd > settings.ccp_task_alert_factor * task.forecast_usd:
        store.set_task(
            db,
            task_id,
            why="cost_alert",
            alerted=True,
            detail={
                "spent_usd": round(task.spent_usd, 6),
                "forecast_usd": round(task.forecast_usd, 6),
                "factor": settings.ccp_task_alert_factor,
            },
        )
        summary.events.append("task_cost_alert")


def advance(
    db: ScopedDB,
    settings: Settings,
    task_id: str,
    *,
    reader: Reader | None = None,
    now: datetime | None = None,
) -> PassSummary:
    task = store.get(db, task_id)
    if task is None:
        raise ValueError(f"no task {task_id!r}")
    summary = PassSummary(task_id, task.status)
    if task.status != "open":
        return summary
    moment = now or datetime.now(UTC)
    if moment > task.deadline:
        _park(db, task, "deadline", summary, deadline=task.deadline.isoformat())
        return summary
    planner = planner_for(task.kind)
    facts = Facts.read(db, moment)
    for it in store.items_of(db, task_id):
        if it.kind != "check" or it.status != "open":
            continue
        summary.checked += 1
        settled = planner.settle(facts, it)
        if settled.status == it.status and settled.outcome == it.outcome:
            continue
        store.set_item(
            db,
            task_id,
            it.item_id,
            status=settled.status,  # type: ignore[arg-type]
            why=settled.why,
            outcome=settled.outcome,
            result=settled.result,
            evidence_ids=settled.evidence_ids,
        )
        summary.changed += 1
    items = store.items_of(db, task_id)
    summary.added = store.add_items(db, task_id, planner.followups(task, items), "followup")
    items = store.items_of(db, task_id) if summary.added else items
    reads = [i for i in items if i.kind == "read" and i.status == "open"]
    summary.reads_waiting = len(reads)
    if reads and reader is not None:
        current = store.get(db, task_id)
        assert current is not None
        if current.spent_usd >= current.max_usd:
            _park(db, current, "budget", summary, spent_usd=round(current.spent_usd, 6))
            return summary
        outcome = reader(db, settings, current, reads)
        summary.ran, summary.usd = outcome.ran, outcome.usd
        summary.changed += outcome.changed
        summary.events.extend(outcome.events)
        _alert(db, settings, task_id, summary)
    items = store.items_of(db, task_id)
    current = store.get(db, task_id)
    assert current is not None
    if items and all(i.settled for i in items):
        outcomes: dict[str, int] = {}
        for i in items:
            outcomes[i.outcome or i.status] = outcomes.get(i.outcome or i.status, 0) + 1
        store.set_task(
            db,
            task_id,
            status="done",
            why="done",
            passes=current.passes + 1,
            stalled_passes=0,
            detail={"items": len(items), "outcomes": outcomes},
        )
        summary.status = "done"
        return summary
    if current.spent_usd >= current.max_usd and any(
        i.kind == "read" and i.status == "open" for i in items
    ):
        _park(db, current, "budget", summary, spent_usd=round(current.spent_usd, 6))
        return summary
    attempted = summary.checked + summary.ran
    progressed = summary.changed > 0 or bool(summary.added)
    stalled = current.stalled_passes + 1 if attempted and not progressed else 0
    store.set_task(
        db,
        task_id,
        why="pass",
        passes=current.passes + 1,
        stalled_passes=stalled,
        detail={
            "checked": summary.checked,
            "changed": summary.changed,
            "added": summary.added,
            "ran": summary.ran,
            "usd": round(summary.usd, 6),
            "stalled_passes": stalled,
        },
    )
    if stalled >= settings.ccp_task_stall_passes:
        _park(db, current, "no_progress", summary, stalled_passes=stalled)
    return summary


def resolve(db: ScopedDB, task_id: str, item_id: str, *, outcome: str, note: str, by: str) -> Item:
    """A person settles an item the runner held for them; the decision is the record."""
    found = store.item(db, task_id, item_id)
    if found is None:
        raise ValueError(f"no item {item_id!r} in task {task_id!r}")
    if found.status not in ("needs_review", "waiting", "open"):
        raise ValueError(f"item {item_id!r} is {found.status}; only a held item is resolved")
    store.set_item(
        db,
        task_id,
        item_id,
        status="final",
        why=f"resolved by {by}",
        outcome=outcome,
        result={**(found.result or {}), "resolved_by": by, "note": note},
    )
    after = store.item(db, task_id, item_id)
    assert after is not None
    return after


def cancel(db: ScopedDB, task_id: str, *, by: str, reason: str) -> None:
    task = store.get(db, task_id)
    if task is None:
        raise ValueError(f"no task {task_id!r}")
    if task.status in ("done", "cancelled"):
        raise ValueError(f"task {task_id!r} is already {task.status}")
    store.set_task(
        db, task_id, status="cancelled", why="cancelled", detail={"by": by, "reason": reason}
    )


def resume(
    db: ScopedDB,
    task_id: str,
    *,
    by: str,
    deadline: datetime | None = None,
    max_usd: float | None = None,
) -> None:
    """A person reopens a parked task; a new deadline or cap is part of the record."""
    task = store.get(db, task_id)
    if task is None:
        raise ValueError(f"no task {task_id!r}")
    if task.status != "parked":
        raise ValueError(f"task {task_id!r} is {task.status}, not parked")
    if deadline is not None or max_usd is not None:
        db.execute(
            "UPDATE tasks SET deadline = COALESCE(%s, deadline),"
            " max_usd = COALESCE(%s, max_usd) WHERE task_id = %s",
            (deadline, max_usd, task_id),
        )
    store.set_task(
        db,
        task_id,
        status="open",
        why="resumed",
        parked_reason=None,
        stalled_passes=0,
        detail={
            "by": by,
            "was": task.parked_reason,
            "deadline": deadline.isoformat() if deadline else None,
            "max_usd": max_usd,
        },
    )


def gaps(db: ScopedDB, task_id: str) -> list[dict[str, Any]]:
    """The gap list is a query over item rows, never a model's summary."""
    out: list[dict[str, Any]] = []
    for it in store.items_of(db, task_id):
        result = it.result or {}
        if it.kind == "check" and it.outcome == "gap":
            for g in result.get("gaps", []):
                out.append({"item_id": it.item_id, "kind": "verdict", **g})
        elif it.kind == "check" and it.status == "open":
            out.append(
                {"item_id": it.item_id, "kind": "not_yet_known", "pending": result.get("pending")}
            )
        elif it.kind == "read" and it.status in ("final", "needs_review"):
            for finding in result.get("findings", []):
                out.append({"item_id": it.item_id, "kind": "reading", **finding})
        elif it.kind == "read":
            out.append({"item_id": it.item_id, "kind": "unread", "status": it.status})
    return out


def advance_open(
    db: ScopedDB, settings: Settings, slot: datetime, reader: Reader | None = None
) -> int:
    """The scheduled job: one pass over every open task."""
    return sum(
        1
        for task in store.list_tasks(db, "open")
        if advance(db, settings, task.task_id, reader=reader, now=slot).status
    )


jobs.JOBS["tasks"] = lambda db, settings, slot: advance_open(db, settings, slot)
