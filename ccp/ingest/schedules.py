"""Schedules on each tenant's wall clock, fired once across processes (spec.md A-108).

    config/tenants.yaml            schedules table (per tenant)          jobs topic
    time_zone, schedules  ──sync──► next_run_at, every, time_zone  ──fire──► one record per slot
                                         │                                    │
    scheduler (worker loop) ─ claims a due row FOR UPDATE SKIP LOCKED ─┘      ▼
       one transaction: schedule_runs row (slot) + the job record +       jobs consumer
       next_run_at moved past now                                         (evaluators/jobs.py)

A slot fires exactly once: the row is locked while it fires, and schedule_runs is keyed by
(tenant, schedule, slot). A scheduler that was down does not replay every missed slot, nor
skip them silently: it runs once for the oldest due slot and records how many later slots it
folded into that run. `daily HH:MM` is the tenant's local time (or the schedule's own zone):
on the spring-forward night a time inside the gap runs after the gap, and on the fall-back
night a repeated time runs once, at its first occurrence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from ccp.config import ScheduleSpec, Settings
from ccp.ingest import event_log
from ccp.ingest.worker import register
from ccp.storage.scoped import ScopedDB

JOBS_TOPIC = "jobs"
MAX_CATCH_UP = 100_000  # slots counted while folding a long outage into one run

_EVERY = re.compile(r"^every (\d+) (minutes|hours)$")
_DAILY = re.compile(r"^daily (\d{2}):(\d{2})$")


def next_slot(every: str, zone: str, after: datetime) -> datetime:
    """The first moment strictly after `after` that the schedule names, in UTC."""
    if m := _EVERY.match(every):
        step = int(m.group(1)) * (60 if m.group(2) == "minutes" else 3600)
        seconds = int(after.timestamp())
        return datetime.fromtimestamp((seconds // step + 1) * step, UTC)
    if m := _DAILY.match(every):
        at = time(int(m.group(1)), int(m.group(2)))
        tz = ZoneInfo(zone)
        day: date = after.astimezone(tz).date()
        for _ in range(3):
            # fold=0: a repeated local time is its first occurrence; a time in the spring
            # gap resolves to the instant after the gap
            moment = datetime.combine(day, at).replace(tzinfo=tz, fold=0).astimezone(UTC)
            if moment > after:
                return moment
            day += timedelta(days=1)
    raise ValueError(f"cannot read schedule {every!r}")


@dataclass(frozen=True)
class Fired:
    schedule_id: str
    job: str
    slot: datetime
    missed: int
    offset: int


def sync(
    cur: psycopg.Cursor[Any], tenant_id: str, specs: tuple[ScheduleSpec, ...], zone: str
) -> None:
    """Bring the tenant's rows in line with its configuration: a new or changed schedule gets
    its next slot from now; one no longer configured is disabled, never deleted."""
    now = datetime.now(UTC)
    for spec in specs:
        tz = spec.time_zone or zone
        cur.execute(
            "INSERT INTO schedules (tenant_id, schedule_id, job, every, time_zone, next_run_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (tenant_id, schedule_id) DO UPDATE SET job = EXCLUDED.job,"
            " every = EXCLUDED.every, time_zone = EXCLUDED.time_zone,"
            " next_run_at = EXCLUDED.next_run_at, enabled = true, updated_at = now()"
            " WHERE schedules.job <> EXCLUDED.job OR schedules.every <> EXCLUDED.every"
            " OR schedules.time_zone <> EXCLUDED.time_zone OR NOT schedules.enabled",
            (tenant_id, spec.id, spec.job, spec.every, tz, next_slot(spec.every, tz, now)),
        )
    cur.execute(
        "UPDATE schedules SET enabled = false, updated_at = now()"
        " WHERE enabled AND NOT (schedule_id = ANY(%s))",
        ([s.id for s in specs],),
    )


def fire_due(cur: psycopg.Cursor[Any], tenant_id: str) -> list[Fired]:
    """Fire every due schedule of the tenant once, in this transaction."""
    rows = cur.execute(
        "SELECT schedule_id, job, every, time_zone, next_run_at FROM schedules"
        " WHERE enabled AND next_run_at <= now() ORDER BY next_run_at"
        " FOR UPDATE SKIP LOCKED"
    ).fetchall()
    now = event_log.db_now(cur)
    fired: list[Fired] = []
    for schedule_id, job, every, zone, slot in rows:
        following = next_slot(every, zone, slot)
        missed = 0
        while following <= now and missed < MAX_CATCH_UP:
            missed += 1
            following = next_slot(every, zone, following)
        if following <= now:  # an outage longer than the cap: start from now
            following = next_slot(every, zone, now)
        offset = event_log.append(
            cur,
            tenant_id=tenant_id,
            topic=JOBS_TOPIC,
            partition_key=f"{tenant_id}:schedule:{schedule_id}",
            source_ts=slot,
            ref=json.dumps(
                {"schedule_id": schedule_id, "job": job, "slot": slot.isoformat(), "missed": missed}
            ),
        )
        inserted = cur.execute(
            "INSERT INTO schedule_runs (tenant_id, schedule_id, slot, missed, job_offset)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING slot",
            (tenant_id, schedule_id, slot, missed, offset),
        ).fetchone()
        if inserted is None:  # cannot happen under the row lock; never fire a slot twice
            raise RuntimeError(f"schedule {schedule_id!r} slot {slot} fired twice")
        cur.execute(
            "UPDATE schedules SET next_run_at = %s, last_run_at = now(), updated_at = now()"
            " WHERE schedule_id = %s",
            (following, schedule_id),
        )
        fired.append(Fired(schedule_id, job, slot, missed, offset))
    return fired


class Scheduler:
    """Worker-loop adapter (`ccp worker run --consumer scheduler`): each pass syncs the
    tenant's configuration and fires its due slots; `next_due` tells the loop when to come
    back, so an idle scheduler wakes for the next slot and nothing in between."""

    name = "scheduler"
    topic = "schedules"

    def __init__(self, db: ScopedDB, settings: Settings) -> None:
        self._db = db
        self.settings = settings
        self.fired: list[Fired] = []

    def run_once(self) -> int:
        tenant = self._db.tenant
        with self._db.transaction() as cur:
            sync(cur, tenant.tenant_id, tenant.settings.schedules, tenant.settings.time_zone)
            fired = fire_due(cur, tenant.tenant_id)
        self.fired.extend(fired)
        return len(fired)

    def next_due(self) -> datetime | None:
        row = self._db.fetch_one("SELECT min(next_run_at) FROM schedules WHERE enabled")
        return row[0] if row else None


register("scheduler")(lambda db, settings: Scheduler(db, settings))
