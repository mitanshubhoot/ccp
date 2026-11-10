"""Layer 9 end to end: `ccp run` populates compliance state; the Jane Smith HR-1 row is fail;
the writer is idempotent and maps trigger reasons onto supersession reasons; the two audit
questions are answerable from the chain."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ccp.compliance_state import reader, run, writer
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators.rule_versions import rule_stamp
from ccp.evaluators.tier2 import Providers
from ccp.evaluators.verdict import Verdict, evaluation_key, latest_per_subject
from ccp.knowledge import graph as knowledge
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from ccp.types import VerdictStatus

pytestmark = pytest.mark.db

JANE = "HR-1|person:hris:person:E1042"
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
KEYLESS = Providers(
    embedder=HashingEmbeddingClient(), reranker=None, llm=None, retrieval=RetrievalConfig()
)


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB], run.RunReport]]:
    try:
        with scratch_database(base_settings, prefix="ccp_state") as settings:
            pool = app_pool(settings, max_size=4)
            try:
                dbs = {
                    t: ScopedDB(pool, TenantContext.for_id(settings, t)) for t in ("acme", "globex")
                }
                before = run.run(dbs["acme"], settings, providers=KEYLESS, now=NOW)
                run.run(dbs["globex"], settings, providers=KEYLESS, now=NOW)
                yield settings, dbs, before
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover
        pytest.skip(f"Postgres not reachable: {exc}")


def test_run_populates_state_and_jane_hr1_is_fail(world) -> None:  # type: ignore[no-untyped-def]
    _, dbs, report = world
    acme = dbs["acme"]
    assert report.landed == 75 and report.evaluated == 11 and report.state_written == 11
    assert report.parked >= 9 and not report.ai_tiers_available  # tier 2 and 3 wait for a key
    assert report.heads == {"fail": 6, "needs_review": 1, "partial": 1, "pass": 3}
    row = reader.current_state(acme, JANE)
    assert row is not None and row.status is VerdictStatus.failed
    assert row.valid_from.isoformat() == "2026-08-21T17:00:00+00:00" and row.reason == "evaluation"
    assert set(row.control_ids) == {"CTL-ACCESS-REVOKE"}
    text = run.format_report(report)
    assert "tenant acme" in text and "NO EVALUATIONS YET" not in text and "parked" in text


def test_rerun_writes_nothing_new(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, first = world
    acme = dbs["acme"]
    heads_before = {r.subject_key: r.row_id for r in reader.heads(acme)}
    again = run.run(acme, settings, providers=KEYLESS, now=NOW)
    assert again.landed == 0 and again.duplicates == 75
    assert again.evaluated == 11 and again.verdict_duplicates == 11 and again.state_written == 0
    assert {r.subject_key: r.row_id for r in reader.heads(acme)} == heads_before
    assert writer.drain(acme).written == 0  # re-consuming the topic writes nothing


def test_rollup_names_the_blocking_subject(world) -> None:  # type: ignore[no-untyped-def]
    _, dbs, _ = world
    soc2 = reader.framework_rollup(dbs["acme"], "soc2")
    assert not soc2.no_evaluations_yet
    by_id = {r.requirement_id: r for r in soc2.requirements}
    assert by_id["CC6.2"].state == "fail" and by_id["CC6.2"].blocking_test_id in ("HR-1", "GH-1")
    assert by_id["CC6.2"].blocking_subject_key is not None and by_id[
        "CC6.2"
    ].blocking_subject_key.endswith("person:hris:person:E1042")
    assert by_id["CC1.4"].state == "pending"  # HR-3 is parked without a key
    iso = reader.framework_rollup(dbs["acme"], "iso27001")
    assert {r.requirement_id: r.state for r in iso.requirements}["A.5.18"] == "fail"


def _verdict(
    db: ScopedDB,
    subject_key: str,
    status: VerdictStatus,
    trigger: datetime,
    reason: str,
    valid_from: datetime,
) -> Verdict:
    return Verdict(
        evaluation_key=evaluation_key(
            db.tenant.tenant_id,
            "HR-1",
            subject_key,
            trigger,
            None,
            None,
            rule=rule_stamp(knowledge.load().tests["HR-1"], db.tenant.settings),
        ),
        tenant_id=db.tenant.tenant_id,
        test_id="HR-1",
        subject_key=subject_key,
        status=status,
        reasoning=f"{reason} test verdict",
        confidence=1.0,
        confidence_cap=1.0,
        tier=1,
        trigger_observed_at=trigger,
        trigger_reason=reason,  # type: ignore[arg-type]
        valid_from=valid_from,
        created_at=trigger,
    )


def test_trigger_reasons_map_onto_supersession_reasons_and_the_chain_answers_both_questions(
    world,
) -> None:  # type: ignore[no-untyped-def]
    _, dbs, _ = world
    acme = dbs["acme"]
    head0 = reader.current_state(acme, JANE)
    assert head0 is not None and head0.reason == "evaluation"
    # valid times must lie in the past for current_state (as of the wall clock) to see them
    hour = timedelta(hours=1)
    stale = _verdict(
        acme, JANE, VerdictStatus.failed, NOW + hour, "revalidate_interval", NOW + hour
    )
    assert evaluations.commit_verdict(acme, stale)
    assert writer.drain(acme).by_reason == {"stale_evidence": 1}
    head1 = reader.current_state(acme, JANE)
    assert head1 is not None and head1.reason == "stale_evidence"
    assert head1.supersedes_row_id == head0.row_id
    human = _verdict(
        acme, JANE, VerdictStatus.passed, NOW + 2 * hour, "human_decision", NOW + 2 * hour
    )
    assert evaluations.commit_verdict(acme, human)
    assert writer.drain(acme).by_reason == {"human_override": 1}
    head2 = reader.current_state(acme, JANE)
    assert head2 is not None and head2.reason == "human_override"
    assert head2.status is VerdictStatus.passed
    # a fact about the past arriving late: valid before the open row's valid_from
    late = _verdict(
        acme, JANE, VerdictStatus.failed, NOW + 3 * hour, "impact", NOW - timedelta(days=10)
    )
    assert evaluations.commit_verdict(acme, late)
    assert writer.drain(acme).by_reason == {"late_event": 1}
    chain = reader.chain(acme, JANE)
    assert [r.reason for r in chain] == [
        "evaluation",
        "stale_evidence",
        "human_override",
        "late_event",
    ]
    assert [r.recorded_seq for r in chain] == sorted(r.recorded_seq for r in chain)
    r2, r3 = chain[2], chain[3]
    # the late fact is filed into history (A-107): bounded at the next newer fact, never the
    # head, so the human's newer decision stays our belief about the present
    assert r3.history and not r2.history and r3.supersedes_row_id is None
    assert r3.valid_to is not None and r3.valid_to <= r2.valid_from
    head3 = reader.current_state(acme, JANE)
    assert head3 is not None and head3.row_id == r2.row_id
    assert [r.row_id for r in reader.heads(acme) if r.subject_key == JANE] == [r2.row_id]
    # "what did we believe at the moment the human decision was recorded?" -> the human's pass
    then = r2.recorded_at
    believed = reader.state_as_of(acme, JANE, valid_at=then, known_at=then)
    assert believed is not None and believed.row_id == r2.row_id
    assert reader.believed_state(acme, JANE, then) is not None
    # "knowing everything we know now, what was true at that same moment?" -> still the pass
    corrected = reader.state_as_of(acme, JANE, valid_at=then, known_at=datetime.now(UTC))
    assert corrected is not None and corrected.row_id == r2.row_id
    # ...and at the moment the late fact describes, the late fail
    past = reader.state_as_of(acme, JANE, valid_at=r3.valid_from, known_at=datetime.now(UTC))
    assert past is not None and past.row_id == r3.row_id
    before_anything = reader.state_as_of(
        acme, JANE, valid_at=NOW - timedelta(days=60), known_at=datetime.now(UTC)
    )
    assert before_anything is None


def test_state_is_tenant_scoped_and_globex_has_its_own_heads(world) -> None:  # type: ignore[no-untyped-def]
    _, dbs, _ = world
    globex = dbs["globex"]
    counts = reader.status_counts(globex)
    assert counts.by_status == {"fail": 3, "pass": 6} and not counts.no_evaluations_yet
    assert reader.current_state(globex, JANE) is None
    assert all(r.subject_key.startswith(("GH-", "HR-")) for r in reader.heads(globex))
    yuki = reader.current_state(globex, "HR-1|person:hris:person:G2005")
    assert yuki is not None and yuki.status is VerdictStatus.passed
    assert len(latest_per_subject(globex)) == 9
