"""MT10 (A-150): long tasks as typed rows, advanced pass by pass by code.

The defect: breadth work ran as nothing at all; there was no record of items, no cap on what
a task may spend or how long it may run, and nothing kept a model from declaring work done.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.storage.scoped import ScopedDB
from ccp.tasks import runner, store
from ccp.tasks.runner import ReadOutcome
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
TIER1 = ("HR-1", "HR-2", "HR-5", "GH-1", "GH-2", "GH-3", "GH-5", "AWS-1", "AWS-2", "IDP-1")
JANE = "HR-1|person:hris:person:E1042"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_tasks", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        evaluations.enqueue_all(acme, settings, now=NOW, tests=TIER1)
        evaluations.drain(acme, settings, now=NOW)
        state_writer.drain(acme)
        yield settings, acme


def _events(acme: ScopedDB, task_id: str, kind: str) -> list[dict]:  # type: ignore[type-arg]
    return [e for e in store.events(acme, task_id) if e["kind"] == kind]


def test_audit_prep_settles_checks_from_verdicts_and_adds_a_family_read(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    task = runner.plan(
        acme,
        settings,
        "audit_prep",
        "SOC 2 CC6 prep",
        {"framework_id": "soc2", "families": ["CC6"], "period_end": "2026-12-31"},
        created_by="auditor-lead",
        now=NOW,
    )
    assert task.forecast_usd == pytest.approx(settings.ccp_task_item_usd)  # one family
    assert task.max_usd == pytest.approx(3 * task.forecast_usd)  # three runs per item
    assert task.deadline == NOW + timedelta(days=14) and task.status == "open"
    assert _events(acme, task.task_id, "planned")[0]["detail"]["items"] == 10

    first = runner.advance(acme, settings, task.task_id, now=NOW)
    items = {i.item_id: i for i in store.items_of(acme, task.task_id)}
    assert items["test:HR-1"].status == "final" and items["test:HR-1"].outcome == "gap"
    assert JANE in {g["subject_key"] for g in items["test:HR-1"].result["gaps"]}  # type: ignore[index]
    assert items["test:HR-1"].evidence_ids  # the verdicts it rests on
    assert items["test:HR-4"].status == "open" and items["test:HR-4"].outcome == "pending"
    assert "family:CC6" not in items  # HR-4 is not known yet, so the family waits
    assert first.status == "open" and first.changed == 10 and first.added == []
    kinds = {g["kind"] for g in runner.gaps(acme, task.task_id)}
    assert {"verdict", "not_yet_known"} <= kinds

    for it in store.items_of(acme, task.task_id):  # a person settles what code could not
        if it.status == "open":
            runner.resolve(acme, task.task_id, it.item_id, outcome="gap", note="none", by="lead")
    second = runner.advance(acme, settings, task.task_id, now=NOW)
    assert second.added == ["family:CC6"] and second.reads_waiting == 1
    read = store.item(acme, task.task_id, "family:CC6")
    assert read is not None and read.kind == "read" and read.status == "open"
    assert "HR-1" in read.inputs["tests"]
    after = store.get(acme, task.task_id)
    assert after is not None and after.status == "open"  # a read is open: not done


def test_three_passes_that_change_nothing_park_the_task(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    task = runner.plan(
        acme,
        settings,
        "audit_prep",
        "CC1",
        {"framework_id": "soc2", "families": ["CC1"]},
        created_by="me",
        now=NOW,
    )  # HR-3 is tier 2 and never evaluated here: pending on every pass
    assert runner.advance(acme, settings, task.task_id, now=NOW).changed == 1
    for expected in (1, 2):
        runner.advance(acme, settings, task.task_id, now=NOW)
        found = store.get(acme, task.task_id)
        assert found is not None and found.stalled_passes == expected and found.status == "open"
    parked = runner.advance(acme, settings, task.task_id, now=NOW)
    assert parked.status == "parked" and parked.parked_reason == "no_progress"
    assert runner.advance(acme, settings, task.task_id, now=NOW).status == "parked"  # no work
    runner.resume(acme, task.task_id, by="lead")
    reopened = store.get(acme, task.task_id)
    assert reopened is not None and reopened.status == "open" and reopened.stalled_passes == 0
    assert [e["detail"]["was"] for e in _events(acme, task.task_id, "resumed")] == ["no_progress"]


def test_the_deadline_parks_and_no_work_starts_after_it(world: tuple[Settings, ScopedDB]) -> None:
    settings, acme = world
    task = runner.plan(
        acme,
        settings,
        "audit_prep",
        "late",
        {"framework_id": "soc2", "families": ["CC8"]},
        created_by="me",
        deadline=NOW + timedelta(hours=1),
        now=NOW,
    )
    out = runner.advance(acme, settings, task.task_id, now=NOW + timedelta(hours=2))
    assert out.status == "parked" and out.parked_reason == "deadline" and out.checked == 0


def test_a_questionnaire_is_done_only_when_code_finds_every_item_final(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    questions = {
        "questions": [
            {
                "id": "1",
                "text": "Is access removed on termination?",
                "control_id": "CTL-ACCESS-REVOKE",
            },
            {"id": "2", "text": "Do you encrypt backups?"},
        ]
    }
    task = runner.plan(
        acme, settings, "questionnaire", "vendor Q", questions, created_by="me", now=NOW
    )
    assert task.forecast_usd == pytest.approx(2 * settings.ccp_task_item_usd)
    runner.advance(acme, settings, task.task_id, now=NOW)
    q1 = store.item(acme, task.task_id, "q:1")
    assert q1 is not None and q1.status == "final" and q1.outcome == "no"  # Jane's HR-1 fails
    found = store.get(acme, task.task_id)
    assert found is not None and found.status == "open"  # q:2 is a read nobody ran
    runner.resolve(acme, task.task_id, "q:2:read", outcome="yes", note="KMS on all", by="ciso")
    done = runner.advance(acme, settings, task.task_id, now=NOW)
    assert done.status == "done"
    assert _events(acme, task.task_id, "done")[0]["detail"]["outcomes"] == {"no": 1, "yes": 1}


def test_spend_alerts_past_one_and_a_half_forecasts_and_parks_at_the_cap(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    questions = {"questions": [{"id": "9", "text": "Do you run a bug bounty?"}]}
    task = runner.plan(
        acme, settings, "questionnaire", "spend", questions, created_by="me", now=NOW
    )
    calls: list[int] = []

    def reader(db, settings, task, items):  # type: ignore[no-untyped-def]
        calls.append(len(items))
        store.charge(db, task.task_id, 0.35)  # a run that costs and changes nothing
        return ReadOutcome(ran=1, usd=0.35)

    first = runner.advance(acme, settings, task.task_id, reader=reader, now=NOW)
    assert "task_cost_alert" in first.events and first.status == "open"
    second = runner.advance(acme, settings, task.task_id, reader=reader, now=NOW)
    assert second.status == "parked" and second.parked_reason == "budget"  # $0.70 of $0.60
    assert "task_cost_alert" not in second.events  # alerted once
    third = runner.advance(acme, settings, task.task_id, reader=reader, now=NOW)
    assert third.status == "parked" and calls == [1, 1]  # nothing starts on a parked task
    assert len(_events(acme, task.task_id, "cost_alert")) == 1
