"""MT8 (A-124): the rollup over the expected population, not only the rows that exist.

The defect: a requirement was pass when every verdict it had was a pass, so a subject never
evaluated, one whose evaluation was parked, or a pass no one had re-checked for months was
invisible, and the requirement read as clean.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ccp.knowledge.graph import Control, Framework, KnowledgeGraph, Requirement
from ccp.knowledge.graph import TestSpec as Spec
from ccp.knowledge.rollup import Coverage, Inventory, rollup
from ccp.storage.bitemporal import StateRow
from ccp.types import VerdictStatus

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
A, B, C = "T-1|repo:a", "T-1|repo:b", "T-1|repo:c"


def _graph() -> KnowledgeGraph:
    requirement = Requirement(requirement_id="R1", title="r", intent="i", clauses=("c",))
    return KnowledgeGraph(
        frameworks={"fw": Framework(framework_id="fw", name="fw", requirements=(requirement,))},
        controls={
            "C1": Control(
                control_id="C1", title="c", description="d", requirements=("fw:R1",), tests=("T-1",)
            )
        },
        tests={
            "T-1": Spec(
                test_id="T-1",
                title="t",
                connector_scope=("github",),
                evaluation="deterministic",
                evidence_types=("repo_settings",),
                subject_kind="repo",
                controls=("C1",),
                rule="t1",
                valid_from_strategy="evidence_observed",
                revalidate_every_days=30,
            )
        },
    )


def _head(subject: str, status: VerdictStatus, age_days: float = 1) -> StateRow:
    recorded = NOW - timedelta(days=age_days)
    return StateRow(
        row_id=f"row-{subject}",
        tenant_id="acme",
        subject_key=subject,
        test_id="T-1",
        control_ids=("C1",),
        status=status,
        verdict_id=f"v-{subject}",
        valid_from=recorded,
        valid_to=None,
        recorded_seq=1,
        recorded_at=recorded,
        supersedes_row_id=None,
        reason="evaluation",
    )


def _coverage(
    subjects: tuple[str, ...] = (A, B),
    parked: tuple[str, ...] = (),
    complete: bool = True,
    fresh: bool = True,
    inventory: bool = True,
) -> Coverage:
    computed = NOW - timedelta(hours=2)
    listed = Inventory(
        subjects=frozenset(subjects),
        complete=complete,
        computed_at=computed,
        fresh_until=computed + timedelta(hours=26 if fresh else 1),
        gaps=() if complete else ("github:repos=partial",),
    )
    return Coverage(
        inventories={"T-1": listed} if inventory else {}, parked=frozenset(parked), now=NOW
    )


def _r1(heads: list[StateRow], coverage: Coverage | None) -> tuple[str, dict[str, int], str | None]:
    (req,) = rollup(_graph(), heads, "fw", coverage).requirements
    return req.state, req.pending_reasons, req.blocking_subject_key


def test_every_expected_subject_passing_is_a_pass() -> None:
    heads = [_head(A, VerdictStatus.passed), _head(B, VerdictStatus.passed)]
    assert _r1(heads, _coverage()) == ("pass", {}, None)


def test_an_expected_subject_with_no_verdict_is_pending_not_a_pass() -> None:
    heads = [_head(A, VerdictStatus.passed)]
    assert _r1(heads, None)[0] == "pass"  # the heads alone could not see it
    assert _r1(heads, _coverage()) == ("pending", {"unassessed": 1}, B)


def test_a_parked_evaluation_is_pending_whether_or_not_a_pass_preceded_it() -> None:
    heads = [_head(A, VerdictStatus.passed), _head(B, VerdictStatus.passed)]
    assert _r1(heads, _coverage(parked=(B,))) == ("pending", {"parked": 1}, B)
    assert _r1(heads[:1], _coverage(parked=(B,))) == ("pending", {"parked": 1}, B)


def test_a_pass_past_its_revalidation_window_is_stale() -> None:
    heads = [_head(A, VerdictStatus.passed), _head(B, VerdictStatus.passed, age_days=32)]
    assert _r1(heads, _coverage()) == ("pending", {"stale": 1}, B)
    heads[1] = _head(B, VerdictStatus.passed, age_days=30.5)  # inside the day's grace
    assert _r1(heads, _coverage())[0] == "pass"


def test_a_fail_is_kept_whatever_else_holds() -> None:
    heads = [_head(A, VerdictStatus.passed), _head(B, VerdictStatus.failed, age_days=90)]
    state, reasons, blocking = _r1(heads, _coverage(parked=(B,), complete=False))
    assert (state, blocking) == ("fail", B)
    assert reasons == {"inventory_incomplete": 1}


def test_a_population_not_listed_whole_or_fresh_cannot_pass() -> None:
    heads = [_head(A, VerdictStatus.passed), _head(B, VerdictStatus.passed)]
    assert _r1(heads, _coverage(complete=False)) == (
        "pending",
        {"inventory_incomplete": 1},
        None,
    )
    assert _r1(heads, _coverage(fresh=False)) == ("pending", {"inventory_stale": 1}, None)
    assert _r1(heads, _coverage(inventory=False)) == ("pending", {"no_inventory": 1}, None)


def test_a_verdict_for_a_subject_the_inventory_did_not_list_still_counts() -> None:
    heads = [
        _head(A, VerdictStatus.passed),
        _head(B, VerdictStatus.passed),
        _head(C, VerdictStatus.failed),  # a repository added since the full run
    ]
    assert _r1(heads, _coverage())[0] == "fail"
