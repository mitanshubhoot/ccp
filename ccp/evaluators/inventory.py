"""Expected subjects, recorded per full run (spec.md A-124).

A requirement used to pass when every verdict it had passed, whatever it did not have: a
repository never evaluated, an evaluation parked for want of a model key, a pass no one had
re-checked in months were all invisible, because the rollup only saw the rows that existed.

The full run now records, for every test, the subjects it is expected to have a verdict for
(its own `subjects` over the tenant's current entities) and whether the source scopes those
subjects are listed from (`subjects_from` in knowledge/tests.yaml) were seen whole and fresh
when they were listed. `coverage` reads the newest inventory of each test and the parked
evaluations, and the rollup counts every expected subject with no current verdict, a parked
one, or a pass past its revalidation window as pending, never as passing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from ulid import ULID

from ccp.evaluators import completeness
from ccp.evaluators.context import EvaluationContext
from ccp.knowledge.rollup import Coverage, Inventory
from ccp.storage.scoped import ScopedDB


def gaps(ctx: EvaluationContext) -> list[str]:
    """The listing scopes of the test in `ctx` that were not seen whole and fresh."""
    stale_after = timedelta(hours=ctx.settings.ccp_sync_stale_hours)
    out = []
    for ref in ctx.spec.subjects_from:
        state = completeness.state_of(ref, ctx.sources, ctx.now, stale_after)
        if state != "complete":
            out.append(f"{ref}={state}")
    return out


def record(cur: psycopg.Cursor[Any], ctx: EvaluationContext, subjects: list[str]) -> None:
    """Append this run's inventory for the test in `ctx`."""
    missing = gaps(ctx)
    stale_after = timedelta(hours=ctx.settings.ccp_sync_stale_hours)
    cur.execute(
        "INSERT INTO subject_inventories (tenant_id, inventory_id, test_id, computed_at,"
        " fresh_until, subjects, complete, gaps) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            ctx.tenant.tenant_id,
            str(ULID()),
            ctx.spec.test_id,
            ctx.now,
            ctx.now + stale_after,  # the next full run is due well before this
            json.dumps(sorted(f"{ctx.spec.test_id}|{s}" for s in subjects)),
            not missing,
            json.dumps(missing),
        ),
    )


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def coverage(db: ScopedDB, now: datetime | None = None) -> Coverage:
    """The newest inventory of every test and the parked subject keys, for the rollup."""
    rows = db.fetch_all(
        "SELECT DISTINCT ON (test_id) test_id, computed_at, fresh_until, subjects, complete,"
        " gaps FROM subject_inventories ORDER BY test_id, computed_at DESC, inventory_id DESC"
    )
    inventories = {
        str(test_id): Inventory(
            subjects=frozenset(str(s) for s in _load(subjects)),
            complete=bool(complete),
            computed_at=computed_at,
            fresh_until=fresh_until,
            gaps=tuple(str(g) for g in _load(gaps_)),
        )
        for test_id, computed_at, fresh_until, subjects, complete, gaps_ in rows
    }
    parked = frozenset(
        str(r[0]) for r in db.fetch_all("SELECT subject_key FROM parked_evaluations")
    )
    return Coverage(inventories=inventories, parked=parked, now=now or datetime.now(UTC))
