"""NT8 (spec.md 5.2, 5.3, A-103): pass^k over k runs, errored runs counted as failures, the AI
test baseline with Wilson intervals, and release gates on confidence bounds: a model-backed
flip is noise to report, a deterministic flip still blocks, and a bound blocks only once a
target has enough cases to say."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ccp.config import REPO_ROOT
from ccp.evals import metrics, regress, runner
from ccp.evals.schema import CaseResult, GoldenCase, RunMetrics, RunReport


def _case(**over: Any) -> GoldenCase:
    base: dict[str, Any] = {
        "id": "c1",
        "level": "component",
        "difficulty": "easy",
        "source": "synthetic",
        "target": "tier2_verdict",
        "expected": {"status": "fail"},
        "requires": ["anthropic"],
        "tenant": "acme",
    }
    return GoldenCase(**{**base, **over})


def _result(passed: bool, status: str | None = "fail", **over: Any) -> CaseResult:
    return CaseResult(
        id=over.pop("id", "c1"),
        level="component",
        target=over.pop("target", "tier2_verdict"),
        passed=passed,
        latency_ms=1.0,
        actual={"status": status} if status else None,
        cost_usd=over.pop("cost_usd", 0.01),
        **over,
    )


def test_wilson_is_honest_at_small_n_and_at_the_edges() -> None:
    assert metrics.wilson(1.0, 10).lower == pytest.approx(0.7225, abs=1e-3)
    assert metrics.wilson(0.9, 10).lower == pytest.approx(0.5958, abs=1e-3)
    assert metrics.wilson(0.0, 35).upper < 0.10 < metrics.wilson(0.0, 30).upper
    empty = metrics.wilson(0.0, 0)
    assert (empty.lower, empty.upper, empty.n) == (0.0, 1.0, 0)


def test_a_model_backed_case_passes_only_when_every_run_does() -> None:
    outcomes = iter([True, True, False, True, True])
    attempts: list[int] = []

    def once() -> CaseResult:
        attempts.append(1)
        ok = next(outcomes)
        return _result(ok, "fail" if ok else "pass", mismatches=[] if ok else ["$.status"])

    folded = runner.repeat(_case(), 5, once)
    assert len(attempts) == 5 and not folded.passed and folded.model_backed
    assert folded.runs == 5 and folded.run_passes == 4 and folded.mismatches == ["$.status"]
    assert folded.run_statuses == ["fail", "fail", "pass", "fail", "fail"]
    assert folded.expected_status == "fail" and folded.cost_usd == pytest.approx(0.05)


def test_an_errored_run_is_a_failed_run_and_a_refusal_is_counted() -> None:
    runs = iter(
        [
            _result(True),
            CaseResult(
                id="c1",
                level="component",
                target="tier2_verdict",
                passed=False,
                latency_ms=0.0,
                error="TerminalModelError: model refused the extraction request",
            ),
        ]
    )
    folded = runner.repeat(_case(), 2, lambda: next(runs))
    assert not folded.passed and folded.run_passes == 1 and folded.refusals == 1
    assert folded.run_statuses == ["fail", None]


def test_deterministic_and_skipped_cases_run_once() -> None:
    calls: list[int] = []

    def once() -> CaseResult:
        calls.append(1)
        return _result(True)

    plain = runner.repeat(_case(requires=[], target="mapper"), 5, once)
    assert len(calls) == 1 and plain.runs == 1 and not plain.model_backed
    skipped = CaseResult(
        id="c1", level="component", target="t", passed=False, skipped=True, latency_ms=0.0
    )
    assert runner.repeat(_case(), 5, lambda: skipped).runs == 1
    judged = _case(requires=[], grading="judge", rubric="verdict_reasoning")
    assert judged.model_backed  # a model grades it, so its grade can flip


def _ai_results(passing: int, total: int, *, runs: int = 5) -> list[CaseResult]:
    out = []
    for i in range(total):
        ok = i < passing
        out.append(
            _result(
                ok,
                id=f"c{i}",
                model_backed=True,
                runs=runs,
                run_passes=runs if ok else runs - 1,
                run_statuses=["fail"] * runs if ok else ["fail"] * (runs - 1) + ["pass"],
                expected_status="fail",
            )
        )
    return out


def test_the_ai_test_baseline_reports_pass_k_false_pass_and_cost() -> None:
    stats = metrics.ai_test_stats(_ai_results(9, 10))["tier2_verdict"]
    assert stats.cases == 10 and stats.runs_per_case == 5
    assert stats.pass_k.estimate == pytest.approx(0.9) and stats.pass_k.n == 10
    assert stats.pass_at_1.estimate == pytest.approx((9 + 0.8) / 10)
    assert stats.false_pass is not None and stats.false_pass.estimate == pytest.approx(0.02)
    assert stats.fail_labelled == 10 and stats.false_fail is None
    assert stats.cost_per_task_usd == pytest.approx(10 * 0.01 / 50)


def _report(run_id: str, cases: list[CaseResult]) -> RunReport:
    now = datetime.now(UTC)
    return RunReport(
        run_id=run_id,
        started_at=now,
        finished_at=now,
        git_commit=None,
        golden_sha256={},
        cases=cases,
        summary={},
        metrics=RunMetrics(ai_tests=metrics.ai_test_stats(cases)),
    )


def test_a_model_backed_flip_is_reported_and_a_deterministic_flip_blocks() -> None:
    before = [_result(True, id="ai", model_backed=True), _result(True, id="det", target="mapper")]
    after = [_result(False, id="ai", model_backed=True), _result(True, id="det", target="mapper")]
    noisy = regress.diff(_report("a", before), _report("b", after))
    assert noisy.noisy_flips == ["ai"] and noisy.flips_to_fail == []
    assert noisy.exit_code == 0
    after[1] = _result(False, id="det", target="mapper")
    assert regress.diff(_report("a", before), _report("c", after)).exit_code == 1


def test_a_bound_below_its_floor_blocks_once_there_are_enough_cases() -> None:
    gates = regress.Gates(default=regress.Gate(pass_k_floor=0.5, min_cases=5))
    good = regress.diff(_report("a", []), _report("b", _ai_results(9, 10)), gates=gates)
    assert good.bound_breaches == [] and good.exit_code == 0  # lower bound 0.60 at 9 of 10
    bad = regress.diff(_report("a", []), _report("c", _ai_results(6, 10)), gates=gates)
    assert bad.exit_code == 1 and bad.bound_breaches[0]["metric"] == "pass^5 lower bound"
    few = regress.diff(_report("a", []), _report("d", _ai_results(1, 4)), gates=gates)
    assert few.exit_code == 0 and few.ungated and "4 cases < 5" in few.ungated[0]


def test_the_false_pass_ceiling_gates_only_with_enough_fail_labelled_cases() -> None:
    gates = regress.Gates(default=regress.Gate(pass_k_floor=0.0, false_pass_ceiling=0.10))
    small = regress.diff(_report("a", []), _report("b", _ai_results(10, 10)), gates=gates)
    assert small.exit_code == 0 and "FAIL-labelled cases < 35" in small.ungated[0]
    many = _ai_results(30, 40)  # ten cases answered PASS once in five on a FAIL label
    big = regress.diff(_report("a", []), _report("c", many), gates=gates)
    assert any(b["metric"] == "false PASS upper bound" for b in big.bound_breaches)


def test_paired_differences_use_tenant_clustered_errors() -> None:
    before = [
        _result(True, id=f"c{i}", model_backed=True, tenant="acme" if i < 3 else "globex")
        for i in range(6)
    ]
    after = [r.model_copy(update={"passed": r.id != "c0", "run_passes": None}) for r in before]
    paired = regress.diff(_report("a", before), _report("b", after)).paired["tier2_verdict"]
    assert paired["n"] == 6 and paired["difference"] == pytest.approx(-1 / 6, abs=1e-3)
    assert paired["lower"] < paired["difference"] < paired["upper"]
    assert regress.clustered_se([0.1, -0.1, 0.2, 0.0], ["a", "a", "b", "b"]) > 0


def test_the_checked_in_gates_load_with_their_defaults() -> None:
    gates = regress.load_gates(REPO_ROOT / "config" / "eval_gates.yaml")
    assert gates.default.pass_k_floor == 0.5 and gates.default.min_fail_cases == 35
    assert regress.load_gates(REPO_ROOT / "config" / "missing.yaml") == regress.Gates()
