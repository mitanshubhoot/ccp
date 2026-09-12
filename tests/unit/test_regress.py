"""`ccp evals regress` exits non-zero on a pass-to-fail flip (a deliberately broken prompt),
on an attack-success-rate increase, and on a p95 breach; everything else is informational."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ccp.evals import regress
from ccp.evals.runner import write_threshold
from ccp.evals.schema import CaseResult, LatencyPercentiles, LevelSummary, RunMetrics, RunReport
from ccp.evaluators import thresholds


def _result(
    case_id: str,
    passed: bool,
    *,
    confidence: float | None = None,
    skipped: bool = False,
    cost: float = 0.0,
) -> CaseResult:
    return CaseResult(
        id=case_id,
        level="component",
        target="tier2_verdict",
        passed=passed and not skipped,
        skipped=skipped,
        latency_ms=10.0,
        actual={"confidence": confidence} if confidence is not None else {},
        cost_usd=cost,
    )


def _report(
    run_id: str,
    results: list[CaseResult],
    *,
    metrics: RunMetrics | None = None,
    hashes: dict[str, str] | None = None,
) -> RunReport:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return RunReport(
        run_id=run_id,
        started_at=now,
        finished_at=now,
        git_commit=None,
        golden_sha256=hashes or {"component.yaml": "abc"},
        cases=results,
        summary={"component": LevelSummary(passed=0, total=0)},
        metrics=metrics or RunMetrics(),
    )


def test_a_broken_prompt_shows_as_a_pass_to_fail_flip_and_fails_the_run() -> None:
    base = _report("b", [_result("gh4", True, confidence=0.9)])
    current = _report("c", [_result("gh4", False, confidence=0.4)])
    reg = regress.diff(base, current)
    assert reg.flips_to_fail == ["gh4"] and reg.exit_code == 1
    assert reg.confidence_deltas == [{"id": "gh4", "before": 0.9, "after": 0.4}]
    assert "pass -> fail: ['gh4']" in regress.format_regression(reg)


def test_an_asr_increase_fails_the_run() -> None:
    base = _report(
        "b", [_result("x", True)], metrics=RunMetrics(asr={"overall": 0.0, "role_play": 0.0})
    )
    current = _report(
        "c", [_result("x", True)], metrics=RunMetrics(asr={"overall": 0.25, "role_play": 0.5})
    )
    reg = regress.diff(base, current)
    assert reg.asr_increases == {"overall": [0.0, 0.25], "role_play": [0.0, 0.5]}
    assert reg.exit_code == 1 and not reg.flips_to_fail


def test_a_p95_breach_fails_the_run() -> None:
    breached = RunMetrics(
        latency={
            "tier2_verdict": LatencyPercentiles(
                count=3, p50=1, p95=9000, p99=9000, slo_p95_ms=100, breached=True
            )
        }
    )
    reg = regress.diff(
        _report("b", [_result("x", True)]), _report("c", [_result("x", True)], metrics=breached)
    )
    assert reg.p95_breaches == ["tier2_verdict"] and reg.exit_code == 1


def test_informational_changes_keep_exit_code_zero() -> None:
    base = _report(
        "b",
        [
            _result("a", False),
            _result("s", True, skipped=True),
            _result("gone", True),
            _result("cost", True, cost=0.01),
        ],
        hashes={"component.yaml": "abc"},
    )
    current = _report(
        "c",
        [
            _result("a", True),
            _result("s", False),
            _result("new", False),
            _result("cost", True, cost=0.03),
        ],
        hashes={"component.yaml": "def", "captured.yaml": "x"},
    )
    reg = regress.diff(base, current)
    assert (
        reg.flips_to_pass == ["a"] and reg.flips_to_fail == []
    )  # a skipped baseline is not a flip
    assert (
        reg.new_failures == ["new"] and reg.added_cases == ["new"] and reg.removed_cases == ["gone"]
    )
    assert reg.golden_changed == ["component.yaml"]  # a new file is not a change
    assert abs(reg.cost_delta_usd - 0.02) < 1e-9
    assert reg.exit_code == 0


def test_reports_round_trip_through_json(tmp_path: Path) -> None:
    report = _report("r", [_result("a", True, confidence=0.7)])
    path = tmp_path / "latest.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    loaded = regress.load_report(path)
    assert loaded.run_id == "r" and loaded.cases[0].actual == {"confidence": 0.7}


def test_write_threshold_is_readable_by_the_thresholds_loader(tmp_path: Path) -> None:
    path = write_threshold(tmp_path / "evaluation.yaml", 0.8, on=datetime(2026, 9, 12, tzinfo=UTC))
    loaded = thresholds.load(path)
    assert loaded.interrupt_threshold == 0.8
    assert 'read_from_curve_on: "2026-09-12"' in path.read_text(encoding="utf-8")
