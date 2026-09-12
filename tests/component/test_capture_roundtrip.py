"""`ccp evals capture <trace_id>` round-trips a trace into a golden case that passes."""

from __future__ import annotations

from pathlib import Path

import pytest

from ccp.config import Settings
from ccp.evals import capture, runner
from ccp.observability import steps


def test_capture_round_trips_a_trace(base_settings: Settings, tmp_path: Path) -> None:
    cases, hashes = runner.load_cases(base_settings.ccp_golden_dir)
    case = next(c for c in cases if c.target == "mapper")
    report = runner.run(base_settings, [case], hashes)
    result = report.cases[0]
    assert result.passed and result.trace_id
    golden = tmp_path / "golden"
    captured, path = capture.capture(steps.log_path(), result.trace_id, golden)
    assert captured.target == "mapper" and captured.input == case.input
    assert captured.tenant == case.tenant and "captured" in captured.tags
    assert "replay material" in (captured.notes or "")
    loaded, _ = runner.load_cases(golden)
    assert [c.id for c in loaded] == [captured.id]
    rerun = runner.run(base_settings, loaded, {})
    assert rerun.all_passed and rerun.cases[0].passed
    again, path_again = capture.capture(steps.log_path(), result.trace_id, golden)
    assert again.id == captured.id and path_again == path
    assert len(runner.load_cases(golden)[0]) == 1  # idempotent


def test_capture_by_run_id_and_unknown_trace(base_settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        capture.capture(steps.log_path(), "no-such-trace", tmp_path)
    line = capture.find_step(steps.log_path(), "no-such-trace")
    assert line is None


def test_expectation_drops_volatile_fields_and_bounds_confidence() -> None:
    expected = capture.expectation_from(
        {"status": "pass", "confidence": 0.85, "latency_ms": 12.0, "verdict_id": "x", "n": 3}
    )
    assert expected == {"$subset": {"status": "pass", "confidence": {"$gte": 0.75}, "n": 3}}
    assert capture.expectation_from({"latency_ms": 1.0}) == {}
