"""MT2 (A-108): schedules fire once across processes, on each tenant's wall clock.

The defects: nothing ran on a schedule. The staleness clock ran only when a worker loop
happened to poll it, parked evaluations waited for someone to type `ccp worker unpark`
after the budget day rolled over (budget_gate.py promised it), and nothing knew a tenant's
time zone.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ccp.config import ScheduleSpec, Settings, TenantSettings
from ccp.evaluators import jobs
from ccp.ingest import dispatch, event_log, schedules, worker
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext, TenantId

pytestmark = pytest.mark.db


@pytest.fixture
def own(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with scratch_database(base_settings, prefix="ccp_sched") as settings:
            pool = app_pool(settings, max_size=8)
            try:
                yield settings, ScopedDB(pool, TenantContext.for_id(settings, "acme"))
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


def _with(db: ScopedDB, **changes: object) -> ScopedDB:
    """The same tenant with other settings (time zone, schedules)."""
    tenant = db.tenant
    settings = tenant.settings.model_copy(update=changes)
    return ScopedDB(db._pool, TenantContext(TenantId(tenant.tenant_id), settings))


def _rows(db: ScopedDB) -> dict[str, tuple[str, datetime, bool]]:
    return {
        str(r[0]): (str(r[1]), r[2], bool(r[3]))
        for r in db.fetch_all("SELECT schedule_id, time_zone, next_run_at, enabled FROM schedules")
    }


def test_the_defaults_sync_on_the_tenants_wall_clock(own: tuple[Settings, ScopedDB]) -> None:
    _, acme = own
    ny = _with(acme, time_zone="America/New_York")
    scheduler = schedules.Scheduler(ny, own[0])
    assert scheduler.run_once() == 0  # nothing due yet: every first slot lies ahead
    rows = _rows(ny)
    assert set(rows) == {"clock", "unpark", "full-run"}
    zone, full_run_at, enabled = rows["full-run"]
    assert zone == "America/New_York" and enabled
    local = full_run_at.astimezone(schedules.ZoneInfo("America/New_York"))
    assert (local.hour, local.minute) == (2, 0)
    assert rows["unpark"][0] == "UTC"  # the budget day is a UTC day
    assert scheduler.next_due() == min(r[1] for r in rows.values())


def test_a_slot_fires_once_across_racing_schedulers(own: tuple[Settings, ScopedDB]) -> None:
    settings, acme = own
    schedules.Scheduler(acme, settings).run_once()  # sync the defaults
    acme.execute(
        "UPDATE schedules SET next_run_at = now() - interval '1 minute' WHERE schedule_id = 'clock'"
    )
    barrier = threading.Barrier(4)
    fired: list[int] = []

    def race() -> None:
        scheduler = schedules.Scheduler(acme, settings)
        barrier.wait()
        fired.append(scheduler.run_once())

    threads = [threading.Thread(target=race) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(fired) == [0, 0, 0, 1]
    runs = acme.fetch_all("SELECT schedule_id, missed FROM schedule_runs")
    assert runs == [("clock", 0)]
    assert acme.fetch_one("SELECT count(*) FROM event_log WHERE topic = 'jobs'") == (1,)


def test_missed_slots_fold_into_one_run_that_counts_them(own: tuple[Settings, ScopedDB]) -> None:
    settings, acme = own
    schedules.Scheduler(acme, settings).run_once()
    three_days = datetime.now(UTC) - timedelta(days=3, hours=1)
    acme.execute(
        "UPDATE schedules SET next_run_at = %s WHERE schedule_id = 'full-run'", (three_days,)
    )
    scheduler = schedules.Scheduler(acme, settings)
    assert scheduler.run_once() == 1
    (run,) = scheduler.fired
    assert run.schedule_id == "full-run" and run.slot == three_days and run.missed == 3
    next_at = _rows(acme)["full-run"][1]
    assert datetime.now(UTC) < next_at <= datetime.now(UTC) + timedelta(days=1)


def test_a_changed_schedule_moves_and_a_removed_one_is_disabled(
    own: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = own
    schedules.Scheduler(acme, settings).run_once()
    changed = _with(
        acme,
        schedules=(ScheduleSpec(id="clock", job="clock", every="every 2 hours"),),
    )
    synced_at = datetime.now(UTC)
    schedules.Scheduler(changed, settings).run_once()
    rows = _rows(acme)
    # the changed schedule takes its next slot from the new form (the next even hour); the
    # old quarter-hour slot can coincide with it, so the slot itself is checked, not a change
    every = acme.fetch_one("SELECT every FROM schedules WHERE schedule_id = 'clock'")
    assert every == ("every 2 hours",)
    assert rows["clock"][1] == schedules.next_slot("every 2 hours", "UTC", synced_at)
    assert rows["clock"][2] and not rows["unpark"][2] and not rows["full-run"][2]


def test_the_unpark_job_runs_from_the_jobs_topic(own: tuple[Settings, ScopedDB]) -> None:
    settings, acme = own
    with acme.transaction() as cur:
        parked = event_log.append(
            cur,
            tenant_id="acme",
            topic="evaluations",
            partition_key="acme:HR-5|tenant",
            source_ts=datetime(2026, 9, 1, tzinfo=UTC),
            ref='{"test_id": "HR-5", "subject": "tenant", "trigger_observed_at":'
            ' "2026-09-01T00:00:00+00:00", "reason": "impact"}',
        )
        cur.execute("UPDATE event_log SET status = 'parked' WHERE \"offset\" = %s", (parked,))
    schedules.Scheduler(acme, settings).run_once()
    acme.execute(
        "UPDATE schedules SET next_run_at = now() - interval '1 second'"
        " WHERE schedule_id = 'unpark'"
    )
    assert schedules.Scheduler(acme, settings).run_once() == 1
    consumer = jobs.JobsConsumer(acme, settings)
    assert consumer.run_once() == 1
    assert consumer.ran == [("unpark", "unpark", 1)]
    with acme.transaction() as cur:
        assert event_log.status_of(cur, parked)[0] == "pending"


def test_the_tenant_time_zone_must_be_a_real_zone() -> None:
    with pytest.raises(ValueError, match="time zone"):
        TenantSettings(
            display_name="x",
            github_org="x",
            org_admin_limit=1,
            critical_asset_tags=(),
            termination_revocation_sla_hours=24,
            time_zone="Mars/Olympus_Mons",
        )


def test_the_worker_loop_drives_the_scheduler_until_the_next_slot(
    own: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = own
    dispatcher = worker.Dispatcher(settings, "scheduler", TenantContext.all(settings))
    try:
        assert dispatcher.run(once=True) == 0  # synced every tenant; nothing due yet
        with dispatcher.pool.connection() as conn:
            wait = dispatch.seconds_to_next(conn, "scheduler", ["acme", "globex"])
    finally:
        dispatcher.close()
    assert wait is not None and 0 < wait <= 15 * 60  # the clock's next quarter hour
    assert set(_rows(acme)) == {"clock", "unpark", "full-run"}
