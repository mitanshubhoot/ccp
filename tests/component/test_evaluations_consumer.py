"""The evaluations consumer: idempotent by evaluation key, parks tiers that do not exist yet,
and the clock producer supersedes a stale pass."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from ccp.config import Settings
from ccp.evaluators import clock, runner
from ccp.evaluators.impact import EvaluationRequest, enqueue
from ccp.evaluators.rule_versions import rule_stamp
from ccp.evaluators.verdict import Verdict, evaluation_key, latest_per_subject, persist
from ccp.ingest import event_log
from ccp.knowledge import graph as knowledge
from ccp.normalize import repository
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
YUKI = "person:hris:person:G2005"


def _stamp(settings: Settings, tenant: str, test_id: str) -> str:
    return rule_stamp(knowledge.load().tests[test_id], settings.load_tenants()[tenant])


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB]]]:
    with prepared_tenants(base_settings, "ccp_evalq", ("globex",)) as (settings, dbs):
        yield settings, dbs


def _claim_one(db: ScopedDB, offset: int) -> event_log.LogRecord:
    """Claim this record; anything else the claim took goes straight back to pending, so a
    live lease left behind cannot hold a later test's subject (A-107)."""
    with db.transaction() as cur:
        records = event_log.claim(cur, "evaluations", 100, 300)
        for other in records:
            if other.offset != offset:
                event_log.mark_retry(cur, other.offset, "not this test's", lease=other.lease_token)
    return next(r for r in records if r.offset == offset)


def test_same_record_processed_twice_writes_once(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    settings, dbs = world
    globex = dbs["globex"]
    request = EvaluationRequest("HR-5", "tenant", NOW, reason="impact")
    with globex.transaction() as cur:
        offset = event_log.append(
            cur,
            tenant_id="globex",
            topic="evaluations",
            partition_key="globex:HR-5|tenant",
            source_ts=NOW,
            ref=request.to_ref(),
        )
    consumer = runner.EvaluationsConsumer(globex, settings, NOW)
    record = _claim_one(globex, offset)
    consumer.handle(record)  # first delivery
    consumer.handle(record)  # redelivery after a crash between handle and mark_done
    with globex.transaction() as cur:  # the redelivery's worker finishes it
        assert event_log.mark_done(cur, record.offset, lease=record.lease_token)
    key = evaluation_key(
        "globex",
        "HR-5",
        "HR-5|tenant",
        NOW,
        None,
        None,
        rule=_stamp(settings, "globex", "HR-5"),
        sources="hris:accounts=complete",  # the fixture pull saw the accounts whole (A-123)
    )
    assert globex.fetch_one("SELECT count(*) FROM verdicts WHERE evaluation_key = %s", (key,)) == (
        1,
    )
    assert globex.fetch_one(
        "SELECT count(*) FROM event_log WHERE topic = 'verdicts' AND partition_key = %s",
        ("globex:HR-5|tenant",),
    ) == (1,)
    assert consumer.summary.evaluated == 2 and consumer.summary.duplicates == 1


def test_tier_2_and_3_records_are_parked_not_dead_lettered(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    settings, dbs = world
    globex = dbs["globex"]
    with globex.transaction() as cur:
        offset = event_log.append(
            cur,
            tenant_id="globex",
            topic="evaluations",
            partition_key="globex:HR-4|tenant",
            source_ts=NOW,
            ref=EvaluationRequest("HR-4", "tenant", NOW).to_ref(),
        )
    consumer = runner.EvaluationsConsumer(globex, settings, NOW)
    while consumer.run_once():
        pass
    with globex.transaction() as cur:
        status, attempts, error = event_log.status_of(cur, offset)
    assert status == "parked" and attempts == 0 and "tier 3" in (error or "")
    assert globex.fetch_one("SELECT count(*) FROM dead_letter") == (0,)
    assert runner.parked_count(globex) >= 1
    assert runner.requeue_parked(globex) >= 1
    # still no tier 3 evaluator: it parks again rather than looping
    while consumer.run_once():
        pass
    with globex.transaction() as cur:
        assert event_log.status_of(cur, offset)[0] == "parked"


def test_clock_supersedes_a_stale_pass(world: tuple[Settings, dict[str, ScopedDB]]) -> None:
    settings, dbs = world
    globex = dbs["globex"]
    stale_created = NOW - timedelta(days=45)  # HR-1 revalidates every 30 days
    stale = Verdict(
        evaluation_key=evaluation_key(
            "globex",
            "HR-1",
            f"HR-1|{YUKI}",
            stale_created,
            None,
            None,
            rule=_stamp(settings, "globex", "HR-1"),
        ),
        tenant_id=globex.tenant.tenant_id,
        test_id="HR-1",
        subject_key=f"HR-1|{YUKI}",
        status=VerdictStatus.passed,
        reasoning="an old pass",
        evidence_refs=("hris:person:G2005",),
        confidence=1.0,
        confidence_cap=1.0,
        tier=1,
        trigger_observed_at=stale_created,
        trigger_reason="full_run",
        valid_from=stale_created,
        created_at=stale_created,
    )
    assert persist(globex, stale)
    assert latest_per_subject(globex, "HR-1")[0].verdict_id == stale.verdict_id

    fresh_tick = clock.tick(globex, now=NOW - timedelta(days=20))  # not yet due
    assert fresh_tick.enqueued == 0
    due = clock.tick(globex, now=NOW)
    assert due.reasons.get("revalidate_interval") == 1
    again = clock.tick(globex, now=NOW)  # coalesced with the pending record
    assert again.enqueued == 0

    summary = runner.drain(globex, settings, now=NOW)
    assert summary.evaluated >= 1 and summary.duplicates == 0
    head = latest_per_subject(globex, "HR-1")[0]
    assert head.verdict_id != stale.verdict_id
    assert head.trigger_reason == "revalidate_interval" and head.status is VerdictStatus.passed
    assert globex.fetch_one(
        "SELECT count(*) FROM verdicts WHERE subject_key = %s", (f"HR-1|{YUKI}",)
    ) == (2,)  # the stale row stays; append-only


def test_clock_flags_stale_evidence_by_max_age(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    settings, dbs = world
    globex = dbs["globex"]
    evidence = globex.fetch_one(
        "SELECT entity_id, observed_at FROM current_evidence"
        " WHERE evidence_type = 'policy_document' ORDER BY observed_at LIMIT 1"
    )
    assert evidence is not None
    recent = NOW - timedelta(days=1)
    verdict = Verdict(
        evaluation_key=evaluation_key(
            "globex",
            "GH-4",
            "GH-4|tenant",
            recent,
            "gh4_extract_v1",
            "m",
            rule=_stamp(settings, "globex", "GH-4"),
        ),
        tenant_id=globex.tenant.tenant_id,
        test_id="GH-4",
        subject_key="GH-4|tenant",
        status=VerdictStatus.passed,
        reasoning="recent verdict on old evidence",
        evidence_refs=(str(evidence[0]),),
        confidence=0.9,
        confidence_cap=0.9,
        tier=2,
        model="m",
        prompt_version="gh4_extract_v1",
        trigger_observed_at=recent,
        valid_from=recent,
        created_at=recent,
    )
    assert persist(globex, verdict)
    far_future = evidence[1] + timedelta(days=366)  # GH-4 evidence_max_age_days = 365
    summary = clock.tick(globex, now=far_future)
    assert summary.reasons.get("stale_evidence", 0) >= 1
    row = globex.fetch_one(
        "SELECT ref FROM event_log WHERE topic = 'evaluations' AND partition_key = %s"
        ' ORDER BY "offset" DESC LIMIT 1',
        ("globex:GH-4|tenant",),
    )
    assert row is not None and EvaluationRequest.from_ref(row[0]).reason == "stale_evidence"


def test_enqueue_all_then_drain_reports_statuses(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    settings, dbs = world
    globex = dbs["globex"]
    runner.drain(globex, settings, now=NOW)  # whatever the clock tests above left pending
    enqueued = runner.enqueue_all(globex, settings, now=NOW, tests=("GH-3", "HR-5"))
    assert enqueued == 3  # two repos + the tenant
    summary = runner.drain(globex, settings, now=NOW)
    assert summary.evaluated == 3
    assert summary.by_status == {"pass": 2, "fail": 1}
    trigger = repository.latest_observed_at(globex)  # what enqueue_all used as the trigger
    assert trigger is not None
    with globex.transaction() as cur:
        enqueue(
            cur, "globex", EvaluationRequest("GH-3", "repo:github:asset:R_kgDOGlobexM002", trigger)
        )
    assert runner.drain(globex, settings, now=NOW).duplicates == 1  # same trigger, same key
