"""The runner's cross-cutting metrics: reliability curve and ECE, the suggested threshold,
attack-success-rate, nearest-rank percentiles against SLOs, cost from the step log, and the
extraction and judge summaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ccp.evals import metrics
from ccp.evals.schema import CaseResult, CriterionResult, JudgeResult


def _case(
    case_id: str,
    confidence: float | None,
    ok: bool,
    *,
    target: str = "tier2_verdict",
    latency: float = 100.0,
    attack: str | None = None,
    succeeded: bool | None = None,
    cost: float | None = None,
    skipped: bool = False,
) -> CaseResult:
    actual = {"status": "pass"}
    if confidence is not None:
        actual["confidence"] = confidence  # type: ignore[assignment]
    return CaseResult(
        id=case_id,
        level="component",
        target=target,
        passed=ok and not skipped,
        mismatches=[] if ok else ["$.status: expected 'pass', got 'fail'"],
        latency_ms=latency,
        actual=actual,
        attack_class=attack,
        attack_succeeded=succeeded,
        cost_usd=cost,
        skipped=skipped,
    )


def test_reliability_bins_ece_and_suggested_threshold() -> None:
    cases = [_case(f"a{i}", 0.9, True) for i in range(9)]
    cases += [_case("b1", 0.3, False), _case("b2", 0.3, True)]
    cases += [_case("skip", 0.9, True, skipped=True), _case("noconf", None, True)]
    bins, ece, n = metrics.reliability(cases)
    assert n == 11
    assert [(b.lower, b.count) for b in bins] == [(0.2, 2), (0.8, 9)]
    assert bins[0].accuracy == 0.5 and bins[1].accuracy == 1.0
    assert ece is not None and abs(ece - (9 / 11 * 0.1 + 2 / 11 * 0.2)) < 1e-3
    assert metrics.suggested_threshold(bins) == 0.8
    assert metrics.suggested_threshold(bins[:1]) is None  # too few points
    assert metrics.reliability([]) == ([], None, 0)


def test_attack_success_rate_per_class_and_overall() -> None:
    cases = [
        _case("d1", 0.5, True, attack="direct_override", succeeded=False),
        _case("d2", 0.5, False, attack="direct_override", succeeded=True),
        _case("r1", 0.5, True, attack="role_play", succeeded=False),
        _case("s1", 0.5, True, attack="role_play", succeeded=None, skipped=True),
        _case("plain", 0.5, True),
    ]
    rates, counts = metrics.attack_success(cases)
    assert counts == {"direct_override": [1, 2], "role_play": [0, 1], "overall": [1, 3]}
    assert rates["direct_override"] == 0.5 and rates["overall"] == 1 / 3
    assert metrics.attack_success([_case("x", 0.5, True)]) == ({}, {})


def test_percentiles_are_nearest_rank() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert metrics.percentile(values, 50) == 30.0
    assert metrics.percentile(values, 95) == 50.0
    assert metrics.percentile(values, 0) == 10.0
    assert metrics.percentile([], 50) == 0.0


def test_latency_by_target_flags_a_p95_breach() -> None:
    cases = [_case("a", 0.9, True, latency=50.0), _case("b", 0.9, True, latency=150.0)]
    rows = metrics.latency_by_target(cases, {"tier2_verdict": 100.0})
    row = rows["tier2_verdict"]
    assert row.count == 2 and row.p95 == 150.0 and row.slo_p95_ms == 100.0 and row.breached
    assert not metrics.latency_by_target(cases, {})["tier2_verdict"].breached


def test_cost_by_target_sums_only_charged_cases() -> None:
    cases = [
        _case("a", 0.9, True, cost=0.001),
        _case("b", 0.9, True, cost=0.002),
        _case("c", 0.9, True, target="mapper", cost=None),
    ]
    assert metrics.cost_by_target(cases) == {"tier2_verdict": 0.003}


def test_usage_between_reads_model_steps_in_the_window(tmp_path: Path) -> None:
    start = datetime(2026, 9, 12, 10, tzinfo=UTC)
    log = tmp_path / "steps.jsonl"
    inside = {
        "gen_ai.operation.name": "tier2.extract",
        "started_at": (start + timedelta(seconds=1)).isoformat(),
        "gen_ai.request.model": "claude-haiku-4-5",
        "gen_ai.usage.input_tokens": 1000,
        "gen_ai.usage.output_tokens": 100,
    }
    outside = {**inside, "started_at": (start - timedelta(hours=1)).isoformat()}
    no_model = {"gen_ai.operation.name": "eval.x", "started_at": inside["started_at"]}
    log.write_text(
        "\n".join(json.dumps(x) for x in (inside, outside, no_model, {"bad": 1})) + "\nnot json\n",
        encoding="utf-8",
    )
    tokens, usd = metrics.usage_between(log, start, start + timedelta(minutes=1))
    assert tokens == 1100 and abs(usd - 0.0015) < 1e-9
    assert metrics.usage_between(tmp_path / "missing.jsonl", start, start) == (0, 0.0)


def test_extraction_summary_field_accuracy_and_support_precision() -> None:
    good = CaseResult(
        id="e1",
        level="component",
        target="tier2_verdict",
        passed=False,
        mismatches=["$.extracted_fields.timeline_text: expected 'x', got 'y'"],
        latency_ms=1.0,
        actual={"extracted_fields": {"a": 1}, "support_precision": 0.8, "schema_version": "gh4"},
        expected_fields=3,
    )
    other = CaseResult(
        id="e2",
        level="component",
        target="tier2_verdict",
        passed=True,
        latency_ms=1.0,
        actual={"extracted_fields": {"a": 1}, "support_precision": 1.0, "schema_version": "gh4"},
        expected_fields=2,
    )
    summary = metrics.extraction_summary([good, other, _case("plain", 0.5, True)])
    assert summary == {"gh4": {"field_accuracy": 0.8, "support_precision": 0.9, "cases": 2}}


def test_judge_summary_counts_criteria() -> None:
    judged = CaseResult(
        id="j1",
        level="component",
        target="tier2_verdict",
        passed=True,
        latency_ms=1.0,
        judge=JudgeResult(
            criteria=[CriterionResult(name="c1", met=True), CriterionResult(name="c2", met=False)],
            overall_pass=False,
            rubric="verdict_reasoning",
        ),
    )
    summary = metrics.judge_summary([judged, _case("plain", 0.5, True)])
    assert summary == {
        "cases": 1,
        "overall_pass": 0,
        "criteria_met_rate": {"verdict_reasoning:c1": 1.0, "verdict_reasoning:c2": 0.0},
    }
    assert metrics.judge_summary([]) == {"cases": 0}


def test_slos_load_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "slos.yaml"
    path.write_text("p95_ms:\n  tier2_verdict: 1500\n", encoding="utf-8")
    assert metrics.load_slos(path) == {"tier2_verdict": 1500.0}
    assert metrics.load_slos(tmp_path / "missing.yaml") == {}
    assert "pipeline" in metrics.load_slos()  # the repository file


def test_adversarial_cases_do_not_enter_the_reliability_curve() -> None:
    cases = [_case(f"a{i}", 0.9, True) for i in range(10)]
    poisoned = [
        _case(f"p{i}", 0.9, False, attack="direct_override", succeeded=True) for i in range(5)
    ]
    bins, _, n = metrics.reliability(cases + poisoned)
    assert n == 10 and bins[-1].accuracy == 1.0  # the five successful attacks did not count


def test_suggested_threshold_never_drops_below_the_floor() -> None:
    bins, _, _ = metrics.reliability([_case(f"a{i}", 0.05, True) for i in range(12)])
    assert bins[0].lower == 0.0 and bins[0].accuracy == 1.0
    assert metrics.suggested_threshold(bins) == metrics.THRESHOLD_FLOOR
