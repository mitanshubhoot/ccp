"""MT10 (A-150): code planners write a task's items from scoping facts.

The defect: breadth work (audit preparation, a questionnaire's open questions) had no item
list at all, so nothing could say what was done, what was left, or who decided either.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ccp.knowledge import graph as knowledge
from ccp.tasks.planners import AuditPrep, Questionnaire, family_of, planner_for
from ccp.tasks.store import Item, Task

NOW = datetime(2026, 11, 11, tzinfo=UTC)


def _task(kind: str, scope: dict[str, Any]) -> Task:
    return Task(
        "T1",
        kind,
        "t",
        scope,
        "open",
        None,
        1.0,
        0.2,
        NOW,
        3,
        0.0,
        0,
        0,
        False,
        "b",
        "me",
        NOW,
        NOW,
    )


def _item(item_id: str, kind: str, status: str, outcome: str | None, **kw: Any) -> Item:
    return Item(
        "T1",
        item_id,
        kind,
        "q",
        kw.get("inputs", {}),
        status,
        outcome,
        0,
        tuple(kw.get("evidence", ())),
        None,
        None,
        kw.get("result"),
        NOW,
        NOW,
    )


def test_families_are_the_requirement_prefix() -> None:
    assert family_of("CC6.1") == "CC6"
    assert family_of("A.5.15") == "A.5"
    assert family_of("CC1") == "CC1"


def test_audit_prep_writes_one_check_per_in_scope_test() -> None:
    graph = knowledge.load()
    items = AuditPrep().plan(graph, {"framework_id": "soc2", "families": ["CC8", "CC1"]})
    assert [(i.item_id, i.kind) for i in items] == [("test:GH-3", "check"), ("test:HR-3", "check")]
    assert items[0].inputs["families"] == ["CC8"] and items[1].inputs["families"] == ["CC1"]
    assert AuditPrep().expected_reads(items) == 2  # at most one gap read per family
    everything = AuditPrep().plan(graph, {"framework_id": "soc2"})
    assert len(everything) == len({i.item_id for i in everything}) >= 10
    with pytest.raises(ValueError, match="unknown framework"):
        AuditPrep().plan(graph, {"framework_id": "pci"})
    with pytest.raises(ValueError, match="no tests in scope"):
        AuditPrep().plan(graph, {"framework_id": "soc2", "families": ["ZZ9"]})


def test_a_family_read_waits_for_every_check_in_it() -> None:
    task = _task("audit_prep", {"framework_id": "soc2", "period_end": "2026-12-31"})
    gap = {"gaps": [{"subject_key": "HR-1|person:x", "state": "fail", "verdict_id": "V1"}]}
    hr1 = _item(
        "test:HR-1",
        "check",
        "final",
        "gap",
        inputs={"test_id": "HR-1", "families": ["CC6"]},
        result=gap,
    )
    hr4 = _item(
        "test:HR-4", "check", "open", "pending", inputs={"test_id": "HR-4", "families": ["CC6"]}
    )
    gh3 = _item(
        "test:GH-3", "check", "final", "pass", inputs={"test_id": "GH-3", "families": ["CC8"]}
    )
    assert AuditPrep().followups(task, [hr1, hr4, gh3]) == []  # CC6 still open; CC8 clean
    hr4_done = _item(
        "test:HR-4", "check", "final", "pass", inputs={"test_id": "HR-4", "families": ["CC6"]}
    )
    (read,) = AuditPrep().followups(task, [hr1, hr4_done, gh3])
    assert read.item_id == "family:CC6" and read.kind == "read"
    assert read.inputs["gaps"] == [{"test_id": "HR-1", **gap["gaps"][0]}]
    assert read.inputs["verdict_ids"] == ["V1"] and read.inputs["as_of"] == "2026-12-31"


def test_a_questionnaire_reads_only_what_the_verdicts_cannot_answer() -> None:
    graph = knowledge.load()
    scope = {
        "questions": [
            {
                "id": "1",
                "text": "Is access removed on termination?",
                "control_id": "CTL-ACCESS-REVOKE",
            },
            {"id": "2", "text": "Do you encrypt backups?"},
            {"id": "3", "text": "Do you pen test yearly?", "control_id": "CTL-NOPE"},
        ]
    }
    items = Questionnaire().plan(graph, scope)
    assert [(i.item_id, i.kind) for i in items] == [
        ("q:1", "check"),
        ("q:2:read", "read"),
        ("q:3:read", "read"),
    ]
    assert items[0].inputs["tests"] == ["HR-1", "GH-1"]
    assert items[2].inputs["unknown_control"] == "CTL-NOPE"
    undecided = _item("q:1", "check", "final", "undecided", inputs=items[0].inputs, evidence=["V9"])
    (read,) = Questionnaire().followups(_task("questionnaire", scope), [undecided])
    assert read.item_id == "q:1:read" and read.inputs["verdict_ids"] == ["V9"]
    with pytest.raises(ValueError, match="unknown task kind"):
        planner_for("vendor_review")
