"""The deterministic status contract: never "ready" or "compliant" as a judgement (A-36)."""

from __future__ import annotations

from ccp.agent.graphs.copilot import (
    CopilotConfig,
    enforce_status_contract,
    load_config,
    render_rollups,
    says_forbidden,
)

WORDS = ("ready", "compliant")


def test_forbidden_words_are_word_bounded_and_case_insensitive() -> None:
    assert says_forbidden("We are READY.", WORDS) == ["ready"]
    assert says_forbidden("Readiness is a process; the compliance team owns it.", WORDS) == []
    assert says_forbidden("Acme is non-compliant on CC6.1.", WORDS) == ["compliant"]


def test_sentences_with_forbidden_words_are_dropped_and_reported() -> None:
    text = "You are ready for SOC 2! Two requirements fail. Everything else is compliant."
    kept, found = enforce_status_contract(text, WORDS)
    assert kept == "Two requirements fail."
    assert found == ["ready", "compliant"]
    assert enforce_status_contract("Nothing to add.", WORDS) == ("Nothing to add.", [])


def test_rollup_rendering_is_facts_with_the_blocking_test() -> None:
    context = {
        "question": "Are we ready?",
        "get_framework_rollup:abc": {
            "framework_id": "soc2",
            "no_evaluations_yet": False,
            "requirements": [
                {
                    "requirement_id": "CC6.1",
                    "state": "fail",
                    "blocking_test_id": "HR-1",
                    "blocking_subject_key": "HR-1|person:hris:person:E1042",
                    "counts": {"fail": 1, "pass": 3, "pending": 0},
                },
                {"requirement_id": "CC7.1", "state": "pending", "counts": {"pending": 1}},
            ],
        },
        "get_framework_rollup:def": {"framework_id": "iso27001", "no_evaluations_yet": True},
        "list_findings:xyz": {
            "findings": [
                {
                    "test_id": "HR-1",
                    "subject_key": "HR-1|person:hris:person:E1042",
                    "status": "fail",
                },
                {"test_id": "HR-2", "subject_key": "HR-2|person:x", "status": "pass"},
            ]
        },
    }
    text = render_rollups(context)
    assert text.splitlines()[0] == "Computed state (facts, not a judgement):"
    assert (
        "- soc2 CC6.1: fail (fail 1, pass 3); blocking test HR-1 for HR-1|person:hris:person:E1042"
        in text
    )
    assert "- soc2 CC7.1: pending (pending 1)" in text
    assert "- iso27001: no evaluations yet" in text
    assert text.endswith("Failing subjects: HR-1 for HR-1|person:hris:person:E1042")
    assert says_forbidden(text, WORDS) == []


def test_config_defaults_and_file_agree() -> None:
    loaded = load_config()
    assert loaded == CopilotConfig()
    assert loaded.max_actions_per_turn == 2 and loaded.max_tool_calls == 10
