from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ccp.knowledge import graph as kg
from ccp.knowledge.rollup import PRECEDENCE, rollup
from ccp.storage.bitemporal import StateRow
from ccp.types import EvidenceType, VerdictStatus


@pytest.fixture(scope="module")
def graph() -> kg.KnowledgeGraph:
    return kg.load()


def test_graph_shape(graph: kg.KnowledgeGraph) -> None:
    assert set(graph.frameworks) == {"soc2", "iso27001"}
    assert len(graph.frameworks["soc2"].requirements) == 8
    assert len(graph.frameworks["iso27001"].requirements) == 10
    assert len(graph.controls) == 11
    assert sorted(graph.tests) == [
        "AWS-1",
        "AWS-2",
        "AWS-3",
        "GH-1",
        "GH-2",
        "GH-3",
        "GH-4",
        "GH-5",
        "HR-1",
        "HR-2",
        "HR-3",
        "HR-4",
        "HR-5",
    ]
    for control in graph.controls.values():
        assert control.requirements_in("soc2") and control.requirements_in("iso27001")
        assert control.tests


def test_every_requirement_has_full_intent_text(graph: kg.KnowledgeGraph) -> None:
    for fw in graph.frameworks.values():
        for req in fw.requirements:
            assert len(req.intent) > 40
            assert len(req.clauses) >= 3


@pytest.mark.parametrize(
    ("test_id", "tier"),
    [
        ("GH-1", 1),
        ("GH-2", 1),
        ("GH-3", 1),
        ("GH-4", 2),
        ("GH-5", 1),
        ("HR-1", 1),
        ("HR-2", 1),
        ("HR-3", 2),
        ("HR-4", 3),
        ("HR-5", 1),
    ],
)
def test_router_tier_for_every_spec(graph: kg.KnowledgeGraph, test_id: str, tier: int) -> None:
    spec = graph.tests[test_id]
    assert spec.tier == tier
    if tier == 1:
        assert spec.evaluation == "deterministic" and spec.prompt_version is None
    else:
        assert spec.evaluation == "interpretive" and spec.extraction_schema and spec.prompt_version
    if tier == 3:
        assert len(spec.evidence_types) >= 2


def test_reuse_is_structural(graph: kg.KnowledgeGraph) -> None:
    revoke = graph.controls["CTL-ACCESS-REVOKE"]
    assert set(revoke.requirements) == {"soc2:CC6.2", "soc2:CC6.3", "iso27001:A.5.18"}
    assert [t.test_id for t in graph.tests_for_requirement("soc2", "CC6.2")] == ["GH-1", "HR-1"]
    assert "HR-1" in [t.test_id for t in graph.tests_for_requirement("iso27001", "A.5.18")]
    assert graph.tests["HR-4"].evidence_types == (
        EvidenceType.access_review_report,
        EvidenceType.critical_systems_list,
    )


def test_spec_validation_rejects_inconsistencies() -> None:
    base = dict(
        test_id="X-1",
        title="x",
        connector_scope=("github",),
        evidence_types=("policy_document",),
        subject_kind="repo",
        controls=("CTL-SECRETS",),
        rule="r",
        valid_from_strategy="evidence_observed",
    )
    with pytest.raises(ValidationError, match="declare prompt_version"):
        kg.TestSpec(evaluation="interpretive", **base)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="never call a model"):
        kg.TestSpec(evaluation="deterministic", prompt_version="p", extraction_schema="S", **base)  # type: ignore[arg-type]


def test_graph_rejects_dangling_edges(graph: kg.KnowledgeGraph) -> None:
    bad = dict(graph.controls)
    bad["CTL-X"] = kg.Control(
        control_id="CTL-X",
        title="x",
        description="x",
        requirements=("soc2:CC9.9",),
        tests=("GH-1",),
    )
    with pytest.raises(ValidationError, match="no requirement"):
        kg.KnowledgeGraph(frameworks=graph.frameworks, controls=bad, tests=graph.tests)


def _row(test_id: str, subject: str, status: VerdictStatus, verdict_id: str) -> StateRow:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    return StateRow(
        row_id=f"r-{verdict_id}",
        tenant_id="acme",
        subject_key=subject,
        test_id=test_id,
        control_ids=(),
        status=status,
        verdict_id=verdict_id,
        valid_from=now,
        valid_to=None,
        recorded_seq=1,
        recorded_at=now,
        supersedes_row_id=None,
        reason="evaluation",
    )


def test_rollup_precedence_and_blocking(graph: kg.KnowledgeGraph) -> None:
    heads = [
        _row("HR-1", "HR-1|person:E1042", VerdictStatus.failed, "v1"),
        _row("GH-1", "GH-1|person:jsmith", VerdictStatus.passed, "v2"),
        _row("GH-2", "GH-2|org", VerdictStatus.passed, "v3"),
        _row("HR-4", "HR-4|tenant", VerdictStatus.partial, "v4"),
    ]
    soc2 = rollup(graph, heads, "soc2")
    by_id = {r.requirement_id: r for r in soc2.requirements}
    assert by_id["CC6.2"].state == "fail" and by_id["CC6.2"].blocking_test_id == "HR-1"
    assert by_id["CC6.2"].blocking_subject_key == "HR-1|person:E1042"
    assert by_id["CC6.3"].state == "fail"  # HR-1 fail outranks GH-2 pass and HR-4 partial
    assert by_id["CC7.1"].state == "pending" and by_id["CC7.1"].blocking_test_id == "GH-4"
    assert by_id["CC1.4"].state == "pending"
    assert not soc2.no_evaluations_yet
    iso = rollup(graph, heads, "iso27001")
    a518 = {r.requirement_id: r for r in iso.requirements}["A.5.18"]
    assert a518.state == "fail" and "v1" in a518.verdict_ids and "v1" in by_id["CC6.2"].verdict_ids


def test_rollup_with_nothing_evaluated_is_explicit(graph: kg.KnowledgeGraph) -> None:
    result = rollup(graph, [], "soc2")
    assert result.no_evaluations_yet
    assert all(r.state == "pending" for r in result.requirements)
    assert result.summary["pending"] == 8


@pytest.mark.parametrize(
    "worse,better", [(a, b) for i, a in enumerate(PRECEDENCE) for b in PRECEDENCE[i + 1 :]]
)
def test_precedence_pairs(graph: kg.KnowledgeGraph, worse: str, better: str) -> None:
    status_of = {
        "fail": VerdictStatus.failed,
        "needs_review": VerdictStatus.needs_review,
        "partial": VerdictStatus.partial,
        "pass": VerdictStatus.passed,
    }
    heads = []
    if worse != "pending":
        heads.append(_row("HR-1", "HR-1|a", status_of[worse], "va"))
    if better != "pending":
        heads.append(_row("GH-1", "GH-1|b", status_of[better], "vb"))
    # a "pending" side simply has no row for its test
    result = rollup(graph, heads, "soc2")
    cc62 = next(r for r in result.requirements if r.requirement_id == "CC6.2")
    assert cc62.state == worse
