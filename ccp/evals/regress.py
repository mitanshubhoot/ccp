"""Regression against the last run (spec.md Layer 11 "Regression"): cases that flipped,
confidence deltas above 0.15, cost delta, golden files whose hash changed (a golden edit is
distinguishable from a behaviour change), attack-success-rate increases per class, and p95
latency breaches. The exit code is non-zero on a pass-to-fail flip, an ASR increase, or a
p95 breach, which is what a CI job keys on."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ccp.evals.schema import CaseResult, RunReport

CONFIDENCE_DELTA = 0.15


class Regression(BaseModel):
    baseline_run_id: str
    current_run_id: str
    flips_to_fail: list[str] = Field(default_factory=list)
    flips_to_pass: list[str] = Field(default_factory=list)
    new_failures: list[str] = Field(default_factory=list)  # cases absent from the baseline
    confidence_deltas: list[dict[str, Any]] = Field(default_factory=list)
    cost_delta_usd: float = 0.0
    golden_changed: list[str] = Field(default_factory=list)
    asr_increases: dict[str, list[float]] = Field(default_factory=dict)
    p95_breaches: list[str] = Field(default_factory=list)
    added_cases: list[str] = Field(default_factory=list)
    removed_cases: list[str] = Field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if (self.flips_to_fail or self.asr_increases or self.p95_breaches) else 0


def load_report(path: Path) -> RunReport:
    return RunReport.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _executed(r: CaseResult) -> bool:
    return not r.skipped


def _confidence(r: CaseResult) -> float | None:
    value = (r.actual or {}).get("confidence")
    return float(value) if isinstance(value, int | float) else None


def diff(
    baseline: RunReport, current: RunReport, *, confidence_delta: float = CONFIDENCE_DELTA
) -> Regression:
    before = {r.id: r for r in baseline.cases}
    after = {r.id: r for r in current.cases}
    reg = Regression(baseline_run_id=baseline.run_id, current_run_id=current.run_id)
    for case_id, now in after.items():
        was = before.get(case_id)
        if was is None:
            reg.added_cases.append(case_id)
            if _executed(now) and not now.passed:
                reg.new_failures.append(case_id)
            continue
        if _executed(was) and _executed(now):
            if was.passed and not now.passed:
                reg.flips_to_fail.append(case_id)
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
    reg.golden_changed = sorted(
        name
        for name, digest in current.golden_sha256.items()
        if baseline.golden_sha256.get(name) not in (None, digest)
    )
    if baseline.metrics and current.metrics:
        for klass, rate in current.metrics.asr.items():
            previous = baseline.metrics.asr.get(klass)
            if previous is not None and rate > previous:
                reg.asr_increases[klass] = [round(previous, 3), round(rate, 3)]
    if current.metrics:
        reg.p95_breaches = sorted(
            target for target, row in current.metrics.latency.items() if row.breached
        )
    return reg


def format_regression(reg: Regression) -> str:
    lines = [f"regression: {reg.baseline_run_id} -> {reg.current_run_id}"]
    lines.append(f"  pass -> fail: {reg.flips_to_fail or 'none'}")
    lines.append(f"  fail -> pass: {reg.flips_to_pass or 'none'}")
    if reg.new_failures:
        lines.append(f"  new cases failing: {reg.new_failures}")
    if reg.confidence_deltas:
        lines.append(f"  confidence moved by more than {CONFIDENCE_DELTA}:")
        lines.extend(f"    {d['id']}: {d['before']} -> {d['after']}" for d in reg.confidence_deltas)
    lines.append(f"  cost delta: {reg.cost_delta_usd:+.6f} USD")
    lines.append(f"  golden files changed: {reg.golden_changed or 'none'}")
    lines.append(f"  ASR increases: {reg.asr_increases or 'none'}")
    lines.append(f"  p95 breaches: {reg.p95_breaches or 'none'}")
    if reg.added_cases or reg.removed_cases:
        lines.append(f"  cases added {len(reg.added_cases)}, removed {len(reg.removed_cases)}")
    lines.append(f"  exit code: {reg.exit_code}")
    return "\n".join(lines)
