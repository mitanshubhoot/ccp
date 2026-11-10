"""Work hints: which tenants a consumer may have work for (spec.md Layer 2, A-106).

    producer ── append ──► event_log, and pg_notify('ccp_work', '<tenant>:<topic>') at commit
                                                    │
    worker (one consumer, many tenants)             ▼
      LISTEN ccp_work ── a notification for its topic ──► hint (tenant, consumer) ready now
      sweep, at start and every sweep_seconds ──────────► every tenant's hint ready now
      claim the ready hint served longest ago ─► serve one quantum ─► put the hint back:
          more work: ready now (it goes to the back of the line, by served_at)
          a retry or a lease due later: ready then        caught up: idle (NULL)
      nothing ready: wait for a notification, the next due hint, or the next sweep

A hint says "this consumer may have work for this tenant"; the queue itself is the truth.
A lost hint costs at most one sweep interval, never a record: every running worker listens,
a notification waits on its connection until the worker reads it, and the sweep re-checks
every tenant. One tenant's backlog is served a quantum at a time, behind every other ready
tenant, so it cannot starve them; an idle tenant costs nothing between sweeps.

work_hints holds tenant ids and times only, no tenant data. Workers claim across tenants,
so it carries no row-level security; the catalogue lint pins its columns (A-99, A-106).
LISTEN needs a session of its own: behind a transaction-mode pooler (LT9) the listener
connects directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

QUANTUM_RUNS = 5  # run_once calls per turn: up to 50 parallel or 500 ordered records
HOLD_SECONDS = 120.0  # a claimed hint is held this long; a dead worker's hint comes back
IDLE_RETRY_SECONDS = 1.0  # work exists but cannot be taken now (a cap, another worker)
SWEEP_SECONDS = 60.0
ERROR_RETRY_SECONDS = 5.0


@dataclass(frozen=True)
class Claimed:
    tenant_id: str
    held_until: datetime


def mark_ready(conn: psycopg.Connection[Any], consumer: str, tenant_ids: Sequence[str]) -> None:
    """These tenants may have work for the consumer: ready now, a held hint included, so a
    notification that lands while another worker serves the tenant is never lost."""
    if not tenant_ids:
        return
    conn.execute(
        "INSERT INTO work_hints (tenant_id, consumer, ready_at)"
        " SELECT t, %s, now() FROM unnest(%s::text[]) AS t"
        " ON CONFLICT (tenant_id, consumer) DO UPDATE SET ready_at = now()"
        " WHERE work_hints.ready_at IS NULL OR work_hints.ready_at > now()",
        (consumer, list(tenant_ids)),
    )


def claim(
    conn: psycopg.Connection[Any], consumer: str, tenant_ids: Sequence[str], hold_seconds: float
) -> Claimed | None:
    """The ready hint served longest ago, held for `hold_seconds` (a materialized pick, as
    in the queue's own claim: A-104)."""
    row = conn.execute(
        "WITH pick AS MATERIALIZED ("
        "  SELECT tenant_id FROM work_hints"
        "  WHERE consumer = %(c)s AND tenant_id = ANY(%(t)s) AND ready_at <= now()"
        "  ORDER BY served_at NULLS FIRST, tenant_id LIMIT 1 FOR UPDATE SKIP LOCKED"
        ") UPDATE work_hints h SET served_at = clock_timestamp(),"
        " ready_at = clock_timestamp() + make_interval(secs => %(hold)s)"
        " FROM pick WHERE h.consumer = %(c)s AND h.tenant_id = pick.tenant_id"
        " RETURNING h.tenant_id, h.ready_at",
        {"c": consumer, "t": list(tenant_ids), "hold": hold_seconds},
    ).fetchone()
    return Claimed(str(row[0]), row[1]) if row else None


def release(
    conn: psycopg.Connection[Any], consumer: str, claimed: Claimed, ready_at: datetime | None
) -> bool:
    """Put the hint back, unless a notification marked it ready while it was held."""
    cur = conn.execute(
        "UPDATE work_hints SET ready_at = %s"
        " WHERE consumer = %s AND tenant_id = %s AND ready_at = %s",
        (ready_at, consumer, claimed.tenant_id, claimed.held_until),
    )
    return cur.rowcount == 1


def seconds_to_next(
    conn: psycopg.Connection[Any], consumer: str, tenant_ids: Sequence[str]
) -> float | None:
    """How long until the consumer's next hint falls due; None when every hint is idle."""
    row = conn.execute(
        "SELECT extract(epoch FROM min(ready_at) - now()) FROM work_hints"
        " WHERE consumer = %s AND tenant_id = ANY(%s) AND ready_at IS NOT NULL",
        (consumer, list(tenant_ids)),
    ).fetchone()
    return None if row is None or row[0] is None else max(float(row[0]), 0.0)
