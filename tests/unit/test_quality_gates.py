"""MT7 (A-144): a judge gates only when it agrees with experts, refusals are gated per target,
gold sets are sized to the false-PASS bound, and the production jobs never report a pass they
did not run.

The defects: a judge whose agreement with expert labels was never checked could block or
pass a release; refusals counted as nothing; nothing said whether a gold set had enough FAIL
cases to bound a false PASS at all; and nothing watched production between releases.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ccp.config import Settings
from ccp.evals import production, regress, sizing
from ccp.evals.schema import (
    CaseResult,
    CriterionResult,
    GoldenCase,
    JudgeResult,
    LevelSummary,
    RunMetrics,
    RunReport,
)
from ccp.evaluators import jobs

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def _report(run_id: str, cases: list[CaseResult]) -> RunReport:
    return RunReport(
        run_id=run_id,
        started_at=NOW,
        finished_at=NOW,
        git_commit=None,
        golden_sha256={"component.yaml": "abc"},
        cases=cases,
        summary={"component": LevelSummary(passed=0, total=0)},
        metrics=RunMetrics(),
    )


def _judged(passed: bool) -> CaseResult:
    verdict = JudgeResult(
        criteria=[CriterionResult(name="c1", met=passed, evidence="r")],
        overall_pass=passed,
    )
    return CaseResult(
        id="judged",
        level="component",
        target="tier2_verdict",
        passed=passed,
        latency_ms=1.0,
        judge=verdict,
    )


def test_an_uncalibrated_judge_does_not_gate() -> None:
    base, now = _report("b", [_judged(True)]), _report("c", [_judged(False)])
    loose = regress.diff(base, now, judge_agreement=0.80)
    assert not loose.judge_trusted and loose.advisory_judged == ["judged"]
    assert loose.exit_code == 0 and "advisory" in regress.format_regression(loose)
    unknown = regress.diff(base, now, judge_agreement=None)
    assert unknown.exit_code == 0 and unknown.advisory_judged == ["judged"]
    trusted = regress.diff(base, now, judge_agreement=0.90)
    assert trusted.judge_trusted and trusted.flips_to_fail == ["judged"]
    assert trusted.exit_code == 1


def test_a_refusal_rate_over_the_ceiling_blocks() -> None:
    def case(refusals: int) -> CaseResult:
        return CaseResult(
            id=f"r{refusals}",
            level="component",
            target="tier2_verdict",
            passed=True,
            latency_ms=1.0,
            model_backed=True,
            runs=5,
            run_passes=5,
            refusals=refusals,
        )

    calm = regress.diff(_report("b", []), _report("c", [case(0)]))
    assert calm.refusal_breaches == []
    refusing = regress.diff(_report("b", []), _report("c", [case(1)]))
    assert refusing.refusal_breaches[0]["value"] == 0.2 and refusing.exit_code == 1


def test_the_latest_calibration_says_whether_the_judge_gates(tmp_path: Path) -> None:
    assert regress.latest_judge_agreement(tmp_path) is None
    for stamp, value in (("20260901T000000Z", 0.7), ("20260910T000000Z", 0.88)):
        (tmp_path / f"calibration-{stamp}.json").write_text(
            json.dumps({"overall": {"agreement": value}}), encoding="utf-8"
        )
    assert regress.latest_judge_agreement(tmp_path) == 0.88
    assert regress.load_gates().judge_min_agreement == 0.85


def _golden(case_id: str, target: str, status: str) -> GoldenCase:
    return GoldenCase(
        id=case_id,
        level="component",
        difficulty="easy",
        source="manual",
        target=target,
        expected={"status": status},
    )


def test_a_gold_set_is_sized_to_its_false_pass_bound() -> None:
    assert sizing.fail_cases_needed(0.02, 0.95) == 149
    assert sizing.upper_bound(149) <= 0.02 < sizing.upper_bound(148)
    cases = [_golden(f"f{i}", "tier2_verdict", "fail") for i in range(30)] + [
        _golden(f"p{i}", "tier2_verdict", "pass") for i in range(40)
    ]
    (row,) = sizing.size(cases, 0.10)
    assert (row.fail_cases, row.needed) == (30, 29)
    assert row.findings == ["FAIL share 43% < 50%"]
    (strict,) = sizing.size(cases, 0.02)
    assert "30 FAIL cases, 149 needed" in strict.findings


def test_the_quality_jobs_are_scheduled_and_run_nothing_without_a_key() -> None:
    assert jobs.JOBS["canary"] is production.canary
    assert jobs.JOBS["judged_sample"] is production.judged_sample
    keyless = Settings(anthropic_api_key=None)
    assert production.canary(None, keyless, NOW) == 0  # type: ignore[arg-type]
    assert production.judged_sample(None, keyless, NOW) == 0  # type: ignore[arg-type]
    ids, floor = production.canary_ids()
    assert ids and floor == 1.0
    assert {s.job for s in Settings().load_tenants()["acme"].schedules} >= {
        "canary",
        "judged_sample",
    }
