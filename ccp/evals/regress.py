"""Regression against the last run (spec.md Layer 11 "Regression"): cases that flipped,
confidence deltas above 0.15, cost delta, golden files whose hash changed (a golden edit is
distinguishable from a behaviour change), attack-success-rate increases per class, and p95
latency breaches. The exit code is non-zero on a pass-to-fail flip, an ASR increase, or a
p95 breach, which is what a CI job keys on.

Model-backed cases are gated on confidence bounds, not flips (A-103): their flips are
reported as noise, and a target blocks when the 95% lower bound of its pass^k rate falls
below its floor, or the upper bound of its false-PASS rate rises above its ceiling
(``config/eval_gates.yaml``), once it has enough cases to say. Against a baseline with
the same cases, each AI test also gets a paired difference of per-case pass rates with a
standard error clustered by tenant: the comparison a model or prompt change is signed on."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ccp.config import REPO_ROOT
from ccp.evals.schema import AITestStats, CaseResult, RunReport

CONFIDENCE_DELTA = 0.15
GATES_PATH = REPO_ROOT / "config" / "eval_gates.yaml"


class Gate(BaseModel):
    pass_k_floor: float = 0.5
    false_pass_ceiling: float = 0.10
    min_cases: int = 5
    min_fail_cases: int = 35
    refusal_rate_max: float = 0.05  # model refusals over model-backed runs (A-144)


class Gates(BaseModel):
    default: Gate = Field(default_factory=Gate)
    targets: dict[str, Gate] = Field(default_factory=dict)
    # a judge's verdicts gate a release only when its latest calibration agrees with the
    # expert labels at least this often; below it, judged failures are advisory (A-144)
    judge_min_agreement: float = 0.85

    def for_target(self, target: str) -> Gate:
        return self.targets.get(target, self.default)


def load_gates(path: Path = GATES_PATH) -> Gates:
    if not path.exists():
        return Gates()
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Gates.model_validate(loaded)


class Regression(BaseModel):
    baseline_run_id: str
    current_run_id: str
    flips_to_fail: list[str] = Field(default_factory=list)
    flips_to_pass: list[str] = Field(default_factory=list)
    new_failures: list[str] = Field(default_factory=list)  # cases absent from the baseline
    first_failures: list[str] = Field(default_factory=list)  # skipped before, failing now
    confidence_deltas: list[dict[str, Any]] = Field(default_factory=list)
    cost_delta_usd: float = 0.0
    golden_changed: list[str] = Field(default_factory=list)
    bundle_changed: bool = False  # a new bundle: earned autonomy is re-earned (A-100)
    asr_increases: dict[str, list[float]] = Field(default_factory=dict)
    p95_breaches: list[str] = Field(default_factory=list)
    added_cases: list[str] = Field(default_factory=list)
    removed_cases: list[str] = Field(default_factory=list)
    strict: bool = False  # first failures and new failures block too (the nightly job)
    # model-backed cases: flips, first and new failures are noise to report (A-103)
    noisy_flips: list[str] = Field(default_factory=list)
    bound_breaches: list[dict[str, Any]] = Field(default_factory=list)
    ungated: list[str] = Field(default_factory=list)  # AI tests with too few cases
    paired: dict[str, dict[str, float]] = Field(default_factory=dict)
    judge_agreement: float | None = None  # the latest calibration's overall agreement
    judge_trusted: bool = False
    advisory_judged: list[str] = Field(default_factory=list)  # judged failures, not gating
    refusal_breaches: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def exit_code(self) -> int:
        blocking = (
            self.flips_to_fail
            or self.asr_increases
            or self.p95_breaches
            or self.bound_breaches
            or self.refusal_breaches
        )
        if self.strict:
            blocking = blocking or self.new_failures or self.first_failures
        return 1 if blocking else 0


def load_report(path: Path) -> RunReport:
    return RunReport.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _executed(r: CaseResult) -> bool:
    return not r.skipped


def _confidence(r: CaseResult) -> float | None:
    value = (r.actual or {}).get("confidence")
    return float(value) if isinstance(value, int | float) else None


def diff(
    baseline: RunReport,
    current: RunReport,
    *,
    confidence_delta: float = CONFIDENCE_DELTA,
    strict: bool = False,
    gates: Gates | None = None,
    judge_agreement: float | None = None,
) -> Regression:
    """Strict mode is for the keyed nightly job, whose keyless baseline skipped every keyed
    case: there a case that first runs and fails, or a new case that fails, is the only
    regression signal available, so it blocks."""
    before = {r.id: r for r in baseline.cases}
    after = {r.id: r for r in current.cases}
    reg = Regression(baseline_run_id=baseline.run_id, current_run_id=current.run_id, strict=strict)
    rules = gates or Gates()
    reg.judge_agreement = judge_agreement
    reg.judge_trusted = judge_agreement is not None and judge_agreement >= rules.judge_min_agreement
    for case_id, now in after.items():
        was = before.get(case_id)
        noisy = now.model_backed or (was is not None and was.model_backed)
        if now.judge is not None and not reg.judge_trusted:
            if _executed(now) and not now.passed:  # an uncalibrated judge does not gate
                reg.advisory_judged.append(case_id)
            continue
        if was is None:
            reg.added_cases.append(case_id)
            if _executed(now) and not now.passed:
                (reg.noisy_flips if noisy else reg.new_failures).append(case_id)
            continue
        if not _executed(was) and _executed(now) and not now.passed:
            # skipped in the baseline (no key), failing now
            (reg.noisy_flips if noisy else reg.first_failures).append(case_id)
        if _executed(was) and _executed(now):
            if was.passed and not now.passed:
                (reg.noisy_flips if noisy else reg.flips_to_fail).append(case_id)
            elif not was.passed and now.passed:
                reg.flips_to_pass.append(case_id)
            b, c = _confidence(was), _confidence(now)
            if b is not None and c is not None and abs(c - b) > confidence_delta:
                reg.confidence_deltas.append(
                    {"id": case_id, "before": round(b, 3), "after": round(c, 3)}
                )
    reg.removed_cases = sorted(set(before) - set(after))
    before_cost = sum(r.cost_usd or 0.0 for r in baseline.cases)
    after_cost = sum(r.cost_usd or 0.0 for r in current.cases)
    reg.cost_delta_usd = round(after_cost - before_cost, 6)
    reg.bundle_changed = (
        baseline.bundle_sha256 is not None
        and current.bundle_sha256 is not None
        and baseline.bundle_sha256 != current.bundle_sha256
    )
    reg.golden_changed = sorted(
        name
        for name, digest in current.golden_sha256.items()
        if baseline.golden_sha256.get(name) not in (None, digest)
    )
    if baseline.metrics and current.metrics:
        for klass, rate in current.metrics.asr.items():
            previous = baseline.metrics.asr.get(klass, 0.0)  # unseen before: any success counts
            if rate > previous:
                reg.asr_increases[klass] = [round(previous, 3), round(rate, 3)]
    if current.metrics:
        reg.p95_breaches = sorted(
            target for target, row in current.metrics.latency.items() if row.breached
        )
        for target, stats in sorted(current.metrics.ai_tests.items()):
            breaches, ungated = check_bounds(stats, rules.for_target(target))
            reg.bound_breaches.extend(breaches)
            if ungated:
                reg.ungated.append(f"{target}: {ungated}")
    reg.paired = paired_differences(baseline, current)
    reg.refusal_breaches = refusal_breaches(current, rules)
    return reg


def refusal_breaches(current: RunReport, gates: Gates) -> list[dict[str, Any]]:
    """Targets whose model refused more of their runs than their gate allows (A-144)."""
    runs: dict[str, int] = {}
    refused: dict[str, int] = {}
    for r in current.cases:
        if r.model_backed and _executed(r):
            runs[r.target] = runs.get(r.target, 0) + max(1, r.runs)
            refused[r.target] = refused.get(r.target, 0) + r.refusals
    out: list[dict[str, Any]] = []
    for target in sorted(runs):
        rate = refused[target] / runs[target]
        limit = gates.for_target(target).refusal_rate_max
        if rate > limit:
            out.append({"target": target, "metric": "refusal rate", "value": rate, "limit": limit})
    return out


def latest_judge_agreement(runs_dir: Path) -> float | None:
    """The overall agreement of the newest judge calibration (`ccp evals calibrate`)."""
    reports = sorted(runs_dir.glob("calibration-*.json"))
    if not reports:
        return None
    found = json.loads(reports[-1].read_text(encoding="utf-8"))
    value = (found.get("overall") or {}).get("agreement")
    return float(value) if isinstance(value, int | float) else None


def check_bounds(stats: AITestStats, gate: Gate) -> tuple[list[dict[str, Any]], str]:
    """Breaches of the floor and ceiling, and why a bound was not gated, if it was not."""
    breaches: list[dict[str, Any]] = []
    reasons: list[str] = []
    if stats.cases < gate.min_cases:
        reasons.append(f"pass^k on {stats.cases} cases < {gate.min_cases}")
    elif stats.pass_k.lower < gate.pass_k_floor:
        breaches.append(
            {
                "target": stats.target,
                "metric": f"pass^{stats.runs_per_case} lower bound",
                "value": stats.pass_k.lower,
                "limit": gate.pass_k_floor,
            }
        )
    if stats.false_pass is not None:
        if stats.fail_labelled < gate.min_fail_cases:
            reasons.append(
                f"false PASS on {stats.fail_labelled} FAIL-labelled cases < {gate.min_fail_cases}"
            )
        elif stats.false_pass.upper > gate.false_pass_ceiling:
            breaches.append(
                {
                    "target": stats.target,
                    "metric": "false PASS upper bound",
                    "value": stats.false_pass.upper,
                    "limit": gate.false_pass_ceiling,
                }
            )
    return breaches, "; ".join(reasons)


def _rate(r: CaseResult) -> float:
    passes = r.run_passes if r.run_passes is not None else int(r.passed)
    return passes / max(1, r.runs)


def clustered_se(diffs: list[float], clusters: list[str]) -> float:
    """Standard error of the mean of paired differences, clustered (CR1): cases from one
    tenant fail together, so treating them as independent understates the error."""
    n = len(diffs)
    if n < 2:
        return 0.0
    mean = sum(diffs) / n
    sums: dict[str, float] = {}
    for d, c in zip(diffs, clusters, strict=True):
        sums[c] = sums.get(c, 0.0) + (d - mean)
    groups = len(sums)
    if groups < 2:
        return math.sqrt(sum((d - mean) ** 2 for d in diffs) / (n - 1) / n)
    return math.sqrt(groups / (groups - 1) * sum(s * s for s in sums.values())) / n


def paired_differences(baseline: RunReport, current: RunReport) -> dict[str, dict[str, float]]:
    """Per AI test, over cases executed in both runs: the mean change in per-case pass rate
    with a tenant-clustered 95 percent interval. Reported, never gating on its own."""
    before = {r.id: r for r in baseline.cases if _executed(r)}
    rows: dict[str, list[tuple[float, str]]] = {}
    for now in current.cases:
        was = before.get(now.id)
        if was is None or not _executed(now) or not now.model_backed:
            continue
        cluster = now.tenant or was.tenant or now.id
        rows.setdefault(now.target, []).append((_rate(now) - _rate(was), cluster))
    out: dict[str, dict[str, float]] = {}
    for target, pairs in sorted(rows.items()):
        diffs = [d for d, _ in pairs]
        mean = sum(diffs) / len(diffs)
        se = clustered_se(diffs, [c for _, c in pairs])
        out[target] = {
            "n": float(len(diffs)),
            "difference": round(mean, 4),
            "se": round(se, 4),
            "lower": round(mean - 1.96 * se, 4),
            "upper": round(mean + 1.96 * se, 4),
        }
    return out


def format_regression(reg: Regression) -> str:
    lines = [f"regression: {reg.baseline_run_id} -> {reg.current_run_id}"]
    if reg.strict:
        lines.append("  mode: strict (first failures and new failures block)")
    lines.append(f"  pass -> fail: {reg.flips_to_fail or 'none'}")
    if reg.noisy_flips:
        lines.append(f"  model-backed failures (gated by bounds, not flips): {reg.noisy_flips}")
    for b in reg.bound_breaches:
        lines.append(
            f"  BOUND: {b['target']} {b['metric']} {b['value']:.3f} beyond limit {b['limit']:.3f}"
        )
    for note in reg.ungated:
        lines.append(f"  ungated (too few cases): {note}")
    trust = "none calibrated" if reg.judge_agreement is None else f"{reg.judge_agreement:.0%}"
    lines.append(
        f"  judge: {'gates' if reg.judge_trusted else 'advisory'} (agreement with experts {trust})"
    )
    if reg.advisory_judged:
        lines.append(f"  judged failures, advisory only: {reg.advisory_judged}")
    for b in reg.refusal_breaches:
        lines.append(f"  refusal rate {b['target']}: {b['value']:.1%} > {b['limit']:.1%}")
    for target, p in reg.paired.items():
        lines.append(
            f"  paired {target}: {p['difference']:+.3f} (95% {p['lower']:+.3f} to"
            f" {p['upper']:+.3f}, n={int(p['n'])})"
        )
    lines.append(f"  fail -> pass: {reg.flips_to_pass or 'none'}")
    if reg.new_failures:
        lines.append(f"  new cases failing: {reg.new_failures}")
    if reg.first_failures:
        lines.append(
            f"  first executed and failing (skipped in the baseline): {reg.first_failures}"
        )
    if reg.confidence_deltas:
        lines.append(f"  confidence moved by more than {CONFIDENCE_DELTA}:")
        lines.extend(f"    {d['id']}: {d['before']} -> {d['after']}" for d in reg.confidence_deltas)
    lines.append(f"  cost delta: {reg.cost_delta_usd:+.6f} USD")
    lines.append(f"  golden files changed: {reg.golden_changed or 'none'}")
    lines.append(
        "  bundle changed: " + ("yes; earned autonomy is re-earned" if reg.bundle_changed else "no")
    )
    lines.append(f"  ASR increases: {reg.asr_increases or 'none'}")
    lines.append(f"  p95 breaches: {reg.p95_breaches or 'none'}")
    if reg.added_cases or reg.removed_cases:
        lines.append(f"  cases added {len(reg.added_cases)}, removed {len(reg.removed_cases)}")
    lines.append(f"  exit code: {reg.exit_code}")
    return "\n".join(lines)
