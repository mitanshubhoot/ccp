"""MT10 (A-155): what one run's log and events say about how it went."""

from __future__ import annotations

from ccp.agent import harness


def _tool(name: str, error: bool = False) -> tuple[str, dict, dict, None]:  # type: ignore[type-arg]
    return ("tool", {"name": name, "arguments": {}}, {"is_error": error}, None)


def _model(tokens: int, usd: float) -> tuple[str, dict, dict, dict]:  # type: ignore[type-arg]
    return ("model", {}, {}, {"input_tokens": tokens, "output_tokens": 0, "usd": usd})


def test_a_clean_run_passes_first_time_and_is_accepted() -> None:
    s = harness.summarize([], [_model(1000, 0.01), _tool("search_evidence"), _model(500, 0.005)])
    assert s.stops == [] and s.model_calls == 2 and s.tokens == 1500
    assert s.usd == 0.015 and s.first_pass and s.accepted
    assert s.tool_calls == {"search_evidence": {"calls": 1, "errors": 0, "repeated": 0}}
    assert s.correction_succeeded is None


def test_stops_errors_repeats_and_a_failed_round_are_counted() -> None:
    events = [
        "tool_call_repeated:search_evidence",
        "tool_loop_stopped:gather:loop_repeat_call",
        "citation_quote_not_found:systems:c1",
        "correction_round:extract_systems:1",
        "correction_failed:1",
        "context_swapped:gather:search_evidence",
        "context_dropped:answer:3",
        "context_ceiling:answer",
    ]
    steps = [_tool("search_evidence", error=True), _tool("search_evidence"), _tool("get_chunk")]
    s = harness.summarize(events, steps)
    assert s.stops == ["loop_repeat_call", "context_ceiling"]
    assert s.tool_calls["search_evidence"] == {"calls": 3, "errors": 1, "repeated": 1}
    assert s.corrections == 1 and s.correction_succeeded is False
    assert not s.first_pass and not s.accepted
    assert s.context_swaps == 1 and s.context_dropped == 3


def test_a_round_that_worked_is_accepted_but_not_a_first_pass() -> None:
    events = ["citation_unknown_chunk:a:c9", "correction_round:answer:1", "correction_succeeded"]
    s = harness.summarize(events, [])
    assert s.correction_succeeded is True and s.accepted and not s.first_pass


def test_the_loop_line_in_the_context_ceiling_event_is_not_a_second_stop() -> None:
    events = ["context_ceiling:gather:51000>50000", "tool_loop_stopped:gather:context_ceiling"]
    assert harness.stops_in(events) == ["context_ceiling"]
