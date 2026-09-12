"""The shared agent core: reducers, the verify node over list-valued fields, the HR-4
compare rule over every combination, and the graph spec's structural checks."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ccp.agent import core
from ccp.agent import state as agent_state
from ccp.agent.graphs import tier3 as tier3_graph
from ccp.evaluators import rules
from ccp.evaluators.extraction import schema_for
from ccp.evaluators.rules import hr4
from ccp.types import VerdictStatus

NOW = datetime(2026, 9, 11, tzinfo=UTC)


def test_reducers_merge_the_way_the_spec_says() -> None:
    assert agent_state.merge_chunks({"a": {"x": 1}}, {"b": {"y": 2}}) == {
        "a": {"x": 1},
        "b": {"y": 2},
    }
    assert agent_state.merge_chunks({"a": {"x": 1}}, {"a": {"x": 9}}) == {"a": {"x": 9}}
    assert agent_state.or_coverage({"t": False}, {"t": True, "u": False}) == {"t": True, "u": False}
    assert (
        agent_state.keep_last("old", None) == "old" and agent_state.keep_last("old", "new") == "new"
    )


def test_verify_node_drops_items_whose_citation_fails() -> None:
    chunks = {
        "c1": {
            "text": "| GitHub | critical | Priya |\n| Okta | critical, tier0 | Dana |",
            "evidence_type": "critical_systems_list",
        },
        "c2": {
            "text": "| GitHub | Priya | 2026-07-10 | 12 |\nApproved by Priya on 2026-07-15.",
            "evidence_type": "access_review_report",
        },
    }
    raw = {
        "extract_systems": {
            "fields": {
                "systems": [
                    {
                        "name": "GitHub",
                        "citation": {"chunk_id": "c1", "quote": "| GitHub | critical | Priya |"},
                    },
                    {
                        "name": "Okta",
                        "citation": {
                            "chunk_id": "c1",
                            "quote": "| Okta | critical, tier0 | Dana |",
                        },
                    },
                    {
                        "name": "Payroll",
                        "citation": {"chunk_id": "c1", "quote": "| Payroll | critical |"},
                    },  # not in chunk
                    {
                        "name": "AWS",
                        "citation": {"chunk_id": "nope", "quote": "| AWS | critical |"},
                    },  # unknown chunk
                ]
            },
            "chunk_ids": ["c1"],
        },
        "extract_scope": {
            "fields": {
                "review_date": "2026-07-15",
                "review_date_citation": {
                    "chunk_id": "c2",
                    "quote": "Approved by Priya on 2026-07-15.",
                },
                "systems_in_scope": [
                    {
                        "name": "GitHub",
                        "citation": {
                            "chunk_id": "c2",
                            "quote": "| GitHub | Priya | 2026-07-10 | 12 |",
                        },
                    }
                ],
            },
            "chunk_ids": ["c2"],
        },
    }
    node = core.make_verify_node(
        "verify",
        {
            "extract_systems": ("systems", "critical_systems", schema_for("CriticalSystemsFields")),
            "extract_scope": (
                "systems_in_scope",
                "systems_in_scope",
                schema_for("AccessReviewFields"),
            ),
        },
    )
    out = node({"retrieved_chunks": chunks, "extracted_raw": raw})
    assert out["extracted"]["critical_systems"] == ["GitHub", "Okta"]
    assert (
        out["extracted"]["systems_in_scope"] == ["GitHub"]
        and out["extracted"]["review_date"] == "2026-07-15"
    )
    assert any(e.startswith("citation_quote_not_found:systems") for e in out["guardrail_events"])
    assert any(e.startswith("citation_unknown_chunk:systems:nope") for e in out["guardrail_events"])
    assert out["citation_stats"] == {"coverage": 1.0, "citation_rate": pytest.approx(4 / 6)}
    assert sum(1 for c in out["verified_citations"] if c["verified"]) == 4


def test_verify_node_marks_a_missing_extraction_as_uncovered() -> None:
    node = core.make_verify_node(
        "verify",
        {"extract_systems": ("systems", "critical_systems", schema_for("CriticalSystemsFields"))},
    )
    out = node({"retrieved_chunks": {}, "extracted_raw": {"extract_systems": None}})
    assert (
        out["extracted"] == {"critical_systems": None} and out["citation_stats"]["coverage"] == 0.0
    )


@pytest.mark.parametrize(
    ("critical", "in_scope", "expected"),
    [
        (None, ["GitHub"], VerdictStatus.needs_review),
        (["GitHub"], None, VerdictStatus.needs_review),
        ([], ["GitHub"], VerdictStatus.needs_review),
        (["GitHub", "Okta"], ["github", "OKTA"], VerdictStatus.passed),
        (["GitHub", "Okta", "Payroll-SaaS"], ["GitHub", "Okta"], VerdictStatus.partial),
        (["GitHub", "Okta"], [], VerdictStatus.failed),
        (["GitHub", "Okta"], ["Slack"], VerdictStatus.failed),
    ],
)
def test_hr4_rule_every_combination(
    critical: list[str] | None, in_scope: list[str] | None, expected: VerdictStatus
) -> None:
    decision = rules.decide(
        "hr4_review_covers_critical",
        {"critical_systems": critical, "systems_in_scope": in_scope, "review_date": "2026-07-15"},
        {},
        NOW,
    )
    assert decision.status is expected
    if expected is VerdictStatus.partial:
        assert "Payroll-SaaS" in decision.reasons[0]


def test_compare_normalizes_names_and_keeps_inventory_order() -> None:
    missing, covered = hr4.compare(["Payroll-SaaS", "GitHub", "AWS-Prod"], ["github", "aws prod"])
    assert missing == ["Payroll-SaaS"] and covered == ["GitHub", "AWS-Prod"]


def test_happy_trajectory_is_the_declared_node_order() -> None:
    assert tier3_graph.TRAJECTORY_HAPPY == (
        "fetch_critical_list",
        "extract_systems",
        "fetch_latest_review",
        "extract_scope",
        "verify_citations",
        "compare",
        "gate",
        "finalize",
    )
