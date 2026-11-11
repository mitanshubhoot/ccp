"""MT10 (A-154): before a read runs, code checks whether the facts already settle it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ccp.knowledge import graph as knowledge
from ccp.knowledge.rollup import Coverage, Inventory
from ccp.storage.bitemporal import StateRow
from ccp.tasks.planners import AuditPrep, Facts, Questionnaire
from ccp.tasks.store import Item
from ccp.types import VerdictStatus

NOW = datetime(2026, 11, 11, tzinfo=UTC)
SUBJECT = "GH-3|repo:github:asset:repo:acme-example/platform"


def _row(status: VerdictStatus) -> StateRow:
    return StateRow(
        row_id="r1",
        tenant_id="acme",
        subject_key=SUBJECT,
        test_id="GH-3",
        control_ids=("CTL-CHANGE-REVIEW",),
        status=status,
        verdict_id="V7",
        valid_from=NOW,
        valid_to=None,
        recorded_seq=1,
        recorded_at=NOW,
        supersedes_row_id=None,
        reason="test",
    )


def _facts(status: VerdictStatus) -> Facts:
    inventory = Inventory(frozenset({SUBJECT}), True, NOW, NOW + timedelta(days=1))
    return Facts(
        knowledge.load(),
        {"GH-3": [_row(status)]},
        Coverage({"GH-3": inventory}, frozenset(), NOW),
    )


def _read(inputs: dict[str, Any]) -> Item:
    return Item("T", "x", "read", "q", inputs, "open", None, 0, (), None, None, None, NOW, NOW)


def test_a_family_read_whose_gaps_closed_is_settled_without_a_model() -> None:
    family = _read({"tests": ["GH-3"], "family": "CC8"})
    assert AuditPrep().recheck(_facts(VerdictStatus.failed), family) is None
    closed = AuditPrep().recheck(_facts(VerdictStatus.passed), family)
    assert closed is not None and closed.status == "final" and closed.outcome == "resolved"
    assert closed.evidence_ids == ("V7",)


def test_a_mapped_question_is_answered_by_its_verdicts_when_they_decide() -> None:
    mapped = _read({"control_id": "CTL-CHANGE-REVIEW", "tests": ["GH-3"]})
    yes = Questionnaire().recheck(_facts(VerdictStatus.passed), mapped)
    assert yes is not None and yes.outcome == "yes"
    no = Questionnaire().recheck(_facts(VerdictStatus.failed), mapped)
    assert no is not None and no.outcome == "no"
    assert Questionnaire().recheck(_facts(VerdictStatus.needs_review), mapped) is None
    assert Questionnaire().recheck(_facts(VerdictStatus.passed), _read({"text": "q"})) is None
