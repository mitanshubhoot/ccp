"""MT10 (A-152): which failures a correction round may repair, and when it runs."""

from __future__ import annotations

from ccp.agent import core
from ccp.agent.bundle import TOOL_ERRORS
from ccp.evaluators.extraction import schema_for


def test_only_repairable_check_failures_are_corrected() -> None:
    events = [
        "citation_quote_not_found:systems:c1",
        "citation_unknown_chunk:root_cause:c9",
        "action_rejected:unknown_action:delete_repo",
        "no_evidence:extract_scope",  # a second try cannot cite what was never gathered
        "injection_marker_detected:c1:ignore previous",
        "tool_loop_stopped:gather:call_cap",
    ]
    assert core.failures_in(events) == events[:3]


def test_the_round_runs_once_and_then_says_how_it_went() -> None:
    assert core.correction_route({"check_failures": ["x"]}) == "correct"
    assert core.correction_route({"check_failures": ["x"], "corrections": 1}) == "next"
    assert core.correction_route({"check_failures": []}) == "next"
    assert core.after_correction({}, ["x"]) == []
    assert core.after_correction({"corrections": 1}, ["x", "y"]) == ["correction_failed:2"]
    assert core.after_correction({"corrections": 1}, []) == ["correction_succeeded"]
    assert core.correction_failed({"corrections": 1, "check_failures": ["x"]})
    assert not core.correction_failed({"corrections": 1, "check_failures": []})


def test_a_failure_belongs_to_the_node_whose_schema_names_it() -> None:
    systems = core.owned_by_fields("extract_systems", schema_for("CriticalSystemsFields"))
    scope = core.owned_by_fields("extract_scope", schema_for("AccessReviewFields"))
    assert systems("citation_quote_not_found:systems:c1")
    assert not scope("citation_quote_not_found:systems:c1")
    assert scope("citation_quote_not_found:systems_in_scope:c2")
    assert scope("schema_validation_failed:extract_scope:2")
    assert not systems("schema_validation_failed:extract_scope:2")


def test_schema_failures_come_from_the_latest_call_only() -> None:
    raw = {"a": {"fields": {}, "schema_errors": 2}, "b": {"fields": {}, "schema_errors": 0}}
    assert core.schema_failures(raw) == ["schema_validation_failed:a:2"]
    assert core.schema_failures({"a": {"fields": {}}}) == []


def test_what_the_model_reads_is_part_of_the_bundle() -> None:
    assert core.REPEAT_NOTE == TOOL_ERRORS["repeated_call"]
    assert core.CORRECTION_NOTE == TOOL_ERRORS["correction"]
    assert "{failures}" in core.CORRECTION_NOTE and "{previous}" in core.CORRECTION_NOTE
