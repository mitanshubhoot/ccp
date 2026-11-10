"""Cross-cutting metrics the runner prints on every run (spec.md Layer 11): the reliability
curve and expected calibration error over verdict confidence, attack-success-rate per
adversarial class, latency percentiles per target against ``config/slos.yaml``, cost per
target from the step log, the extraction accuracy and support-precision suite, the
judged-case summary, and per AI test the pass^k baseline with Wilson intervals (A-103)."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from ccp.config import REPO_ROOT
from ccp.evals.schema import (
    AITestStats,
    CaseResult,
    Interval,
    LatencyPercentiles,
    ReliabilityBin,
)
from ccp.observability import cost

SLOS_PATH = REPO_ROOT / "config" / "slos.yaml"


def load_slos(path: Path = SLOS_PATH) -> dict[str, float]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): float(v) for k, v in (loaded.get("p95_ms") or {}).items()}


def _confidence_cases(cases: list[CaseResult]) -> list[tuple[float, bool]]:
    """(confidence, correct) for every executed case whose output carries a confidence and
    whose expectation names a status; correct means the status expectation held."""
    out: list[tuple[float, bool]] = []
    for r in cases:
        if r.skipped or r.error or not r.actual or r.attack_class is not None:
            continue  # adversarial cases expect an outcome, not a status
        confidence = r.actual.get("confidence")
        if not isinstance(confidence, int | float) or "status" not in r.actual:
            continue
        status_ok = not any(m.startswith("$.status") for m in r.mismatches)
        out.append((float(confidence), status_ok))
    return out


def reliability(
    cases: list[CaseResult], *, bins: int = 5
) -> tuple[list[ReliabilityBin], float | None, int]:
    """Equal-width bins over [0, 1]; ECE = sum over bins of (n_b / N) * |acc_b - conf_b|."""
    points = _confidence_cases(cases)
    if not points:
        return [], None, 0
    width = 1.0 / bins
    rows: list[ReliabilityBin] = []
    ece = 0.0
    for b in range(bins):
        lower = b * width
        upper = 1.0 if b == bins - 1 else (b + 1) * width
        inside = [
            (c, ok) for c, ok in points if (lower <= c < upper) or (b == bins - 1 and c == 1.0)
        ]
        if not inside:
            continue
        mean_conf = sum(c for c, _ in inside) / len(inside)
        accuracy = sum(ok for _, ok in inside) / len(inside)
        rows.append(
            ReliabilityBin(
                lower=round(lower, 3),
                upper=round(upper, 3),
                count=len(inside),
                mean_confidence=round(mean_conf, 4),
                accuracy=round(accuracy, 4),
            )
        )
        ece += (len(inside) / len(points)) * abs(accuracy - mean_conf)
    return rows, round(ece, 4), len(points)


THRESHOLD_FLOOR = 0.5  # the human gate never drops below this, whatever the curve says


def suggested_threshold(
    rows: list[ReliabilityBin],
    *,
    target_accuracy: float = 0.9,
    min_points: int = 10,
    floor: float = THRESHOLD_FLOOR,
) -> float | None:
    """The lowest bin edge from which every bin above reaches the target accuracy, never
    below the floor; None until enough points exist to read a threshold off the curve at
    all."""
    if sum(r.count for r in rows) < min_points:
        return None
    ordered = sorted(rows, key=lambda r: r.lower)
    chosen: float | None = None
    for i, row in enumerate(ordered):
        if all(r.accuracy >= target_accuracy for r in ordered[i:]):
            chosen = row.lower
            break
    return None if chosen is None else max(chosen, floor)


def attack_success(cases: list[CaseResult]) -> tuple[dict[str, float], dict[str, list[int]]]:
    """ASR per adversarial class and overall: succeeded / attempted (skips excluded)."""
    counts: dict[str, list[int]] = {}
    for r in cases:
        if r.attack_class is None or r.skipped or r.attack_succeeded is None:
            continue
        bucket = counts.setdefault(r.attack_class, [0, 0])
        bucket[0] += int(r.attack_succeeded)
        bucket[1] += 1
    overall = [sum(v[0] for v in counts.values()), sum(v[1] for v in counts.values())]
    if overall[1]:
        counts["overall"] = overall
    rates = {k: (v[0] / v[1] if v[1] else 0.0) for k, v in counts.items()}
    return rates, counts


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile; p in [0, 100]."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, int(round(p / 100 * len(ordered) + 0.5)))
    return ordered[min(rank, len(ordered)) - 1]


def latency_by_target(
    cases: list[CaseResult], slos: dict[str, float]
) -> dict[str, LatencyPercentiles]:
    by_target: dict[str, list[float]] = {}
    for r in cases:
        if r.skipped:
            continue
        by_target.setdefault(r.target, []).append(r.latency_ms)
    out: dict[str, LatencyPercentiles] = {}
    for target, values in sorted(by_target.items()):
        p95 = percentile(values, 95)
        slo = slos.get(target)
        out[target] = LatencyPercentiles(
            count=len(values),
            p50=round(percentile(values, 50), 1),
            p95=round(p95, 1),
            p99=round(percentile(values, 99), 1),
            slo_p95_ms=slo,
            breached=slo is not None and p95 > slo,
        )
    return out


def cost_by_target(cases: list[CaseResult]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in cases:
        if r.cost_usd:
            out[r.target] = round(out.get(r.target, 0.0) + r.cost_usd, 6)
    return out


def usage_since(log_path: Path, offset: int) -> tuple[int, float]:
    """Tokens and USD of every model-bearing step appended after byte ``offset`` (the log
    is append-only, so a case's own steps are exactly the bytes it added)."""
    if not log_path.exists():
        return 0, 0.0
    tokens = 0
    usd = 0.0
    with log_path.open("rb") as raw_fh:
        raw_fh.seek(offset)
        tail = raw_fh.read().decode("utf-8", errors="replace")
    for raw in tail.splitlines():
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        model = line.get("gen_ai.request.model")
        if not model:
            continue
        inp = int(line.get("gen_ai.usage.input_tokens") or 0)
        out = int(line.get("gen_ai.usage.output_tokens") or 0)
        tokens += inp + out
        recorded = line.get("ccp.cost_usd")
        if isinstance(recorded, int | float):
            usd += float(recorded)
        else:
            usd += cost.estimate_usd(
                str(model),
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=int(line.get("gen_ai.usage.cache_read_input_tokens") or 0),
                cache_write_tokens=int(line.get("gen_ai.usage.cache_creation_input_tokens") or 0),
            )
    return tokens, round(usd, 6)


def usage_between(log_path: Path, start: datetime, end: datetime) -> tuple[int, float]:
    """Tokens and USD of every model-bearing step in the log started inside [start, end]."""
    if not log_path.exists():
        return 0, 0.0
    tokens = 0
    usd = 0.0
    with log_path.open(encoding="utf-8") as fh:
        for raw in fh:
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            started = line.get("started_at")
            model = line.get("gen_ai.request.model")
            if not started or not model:
                continue
            try:
                at = datetime.fromisoformat(started)
            except ValueError:
                continue
            if not (start <= at <= end):
                continue
            inp = int(line.get("gen_ai.usage.input_tokens") or 0)
            out = int(line.get("gen_ai.usage.output_tokens") or 0)
            read = int(line.get("gen_ai.usage.cache_read_input_tokens") or 0)
            write = int(line.get("gen_ai.usage.cache_creation_input_tokens") or 0)
            tokens += inp + out
            usd += cost.estimate_usd(
                str(model),
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=read,
                cache_write_tokens=write,
            )
    return tokens, round(usd, 6)


def extraction_summary(cases: list[CaseResult]) -> dict[str, Any]:
    """Field accuracy and support precision over cases tagged as extraction cases (the
    runner marks them by target output: ``extracted_fields`` present in the expectation)."""
    per_schema: dict[str, dict[str, float]] = {}
    for r in cases:
        if r.skipped or not r.actual or "extracted_fields" not in (r.actual or {}):
            continue
        if not r.expected_fields:
            continue
        schema = str(r.actual.get("schema_version") or r.target)
        row = per_schema.setdefault(
            schema, {"fields": 0.0, "fields_correct": 0.0, "support_precision_sum": 0.0, "n": 0.0}
        )
        wrong = {m.split(":")[0] for m in r.mismatches if m.startswith("$.extracted_fields")}
        row["fields"] += r.expected_fields
        row["fields_correct"] += max(0, r.expected_fields - len(wrong))
        precision = r.actual.get("support_precision")
        if isinstance(precision, int | float):
            row["support_precision_sum"] += float(precision)
            row["n"] += 1
    out: dict[str, Any] = {}
    for schema, row in per_schema.items():
        out[schema] = {
            "field_accuracy": round(row["fields_correct"] / row["fields"], 4)
            if row["fields"]
            else None,
            "support_precision": round(row["support_precision_sum"] / row["n"], 4)
            if row["n"]
            else None,
            "cases": int(row["n"]),
        }
    return out


def judge_summary(cases: list[CaseResult]) -> dict[str, Any]:
    judged = [r for r in cases if r.judge is not None]
    if not judged:
        return {"cases": 0}
    met: dict[str, list[bool]] = {}
    for r in judged:
        assert r.judge is not None
        for c in r.judge.criteria:
            met.setdefault(f"{r.judge.rubric}:{c.name}", []).append(c.met)
    return {
        "cases": len(judged),
        "overall_pass": sum(1 for r in judged if r.judge and r.judge.overall_pass),
        "criteria_met_rate": {k: round(sum(v) / len(v), 3) for k, v in sorted(met.items())},
    }


Z95 = 1.959964


def wilson(rate: float, n: int, z: float = Z95) -> Interval:
    """The Wilson score interval for a rate observed over n units. It stays inside [0, 1] and
    is honest at small n and at 0 or 1, where the normal interval collapses to a point."""
    if n <= 0:
        return Interval(estimate=0.0, lower=0.0, upper=1.0, n=0)
    p = min(1.0, max(0.0, rate))
    z2 = z * z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return Interval(
        estimate=round(p, 4),
        lower=round(max(0.0, centre - half), 4),
        upper=round(min(1.0, centre + half), 4),
        n=n,
    )


def _status(value: str | None) -> str | None:
    return value.lower() if isinstance(value, str) else None


def ai_test_stats(cases: list[CaseResult]) -> dict[str, AITestStats]:
    """Per model-backed target: pass@1 and pass^k over cases, false PASS among FAIL-labelled
    cases and false FAIL among PASS-labelled ones (share of runs, averaged per case), the
    refusal rate, and cost per task. Skipped cases are left out; errored runs count as
    failures. Every interval uses n = cases: k runs of one case are not k independent
    observations."""
    by_target: dict[str, list[CaseResult]] = {}
    for r in cases:
        if r.model_backed and not r.skipped:
            by_target.setdefault(r.target, []).append(r)
    out: dict[str, AITestStats] = {}
    for target, rows in sorted(by_target.items()):
        k = max(r.runs for r in rows)
        per_case = [
            (r.run_passes if r.run_passes is not None else int(r.passed)) / max(1, r.runs)
            for r in rows
        ]
        all_pass = sum(1 for r in rows if r.passed)
        fails = [r for r in rows if _status(r.expected_status) == "fail"]
        passes = [r for r in rows if _status(r.expected_status) == "pass"]

        def share(group: list[CaseResult], answer: str) -> Interval | None:
            if not group:
                return None
            rates = [
                sum(1 for s in r.run_statuses if _status(s) == answer) / max(1, len(r.run_statuses))
                for r in group
            ]
            return wilson(sum(rates) / len(rates), len(group))

        runs_total = sum(r.runs for r in rows)
        spend = [r.cost_usd for r in rows if r.cost_usd is not None]
        out[target] = AITestStats(
            target=target,
            cases=len(rows),
            runs_per_case=k,
            pass_at_1=wilson(sum(per_case) / len(per_case), len(rows)),
            pass_k=wilson(all_pass / len(rows), len(rows)),
            false_pass=share(fails, "pass"),
            false_fail=share(passes, "fail"),
            fail_labelled=len(fails),
            pass_labelled=len(passes),
            refusal_rate=round(sum(r.refusals for r in rows) / max(1, runs_total), 4),
            cost_per_task_usd=round(sum(spend) / runs_total, 6) if spend else None,
        )
    return out
