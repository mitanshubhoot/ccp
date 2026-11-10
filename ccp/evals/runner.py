"""The eval runner: load golden cases, run each target, compare (and judge), write a report.

    ccp evals run [--level component] [--tag t] [--id case-id] [--suite golden|adversarial|all]

Reports land in evals/runs/{run_id}.json and evals/runs/latest.json with the four level
rows (unit via pytest, then component, workflow, e2e), per-stage metrics, the attack
success rate per adversarial class, the reliability curve with its expected calibration
error, latency percentiles against config/slos.yaml, and cost per target. Cases needing a
database run against one scratch database created for the run. Every target call is one
``eval.<target>`` step in the step log, so any result can become a golden case with
``ccp evals capture <trace_id>``.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml

from ccp.agent.bundle import bundle_sha256
from ccp.config import REPO_ROOT, Settings
from ccp.evals import metrics as metrics_mod
from ccp.evals.adversarial import load_adversarial
from ccp.evals.compare import compare
from ccp.evals.judge import Rubric, SelfGrading, judge_consistently, load_rubric
from ccp.evals.schema import CaseResult, GoldenCase, LevelSummary, RunMetrics, RunReport
from ccp.evals.targets import TARGETS, EvalContext
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import LLMClient, try_build_client
from ccp.observability import steps
from ccp.storage.db import scratch_database

DB_TARGETS = {
    "ingest_land",
    "ingest_ordering",
    "deterministic_verdict",
    "evaluate_tier1",
    "chunker_pages",
    "retrieval_metrics",
    "tier2_verdict",
    "tier3_verdict",
    "investigation",
    "policy_gap",
    "copilot_answer",
    "adversarial_tier2",
    "pipeline",
}
# Targets whose expectations describe a fresh database (first landing, no prior rows) get a
# scratch database of their own; every other database case shares the run's database.
FRESH_DB_TARGETS = {"ingest_land", "pipeline"}
LEVELS = ("component", "workflow", "e2e")
_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")


def load_cases(golden_dir: Path) -> tuple[list[GoldenCase], dict[str, str]]:
    cases: list[GoldenCase] = []
    hashes: dict[str, str] = {}
    for path in sorted(golden_dir.glob("*.yaml")):
        raw = path.read_bytes()
        hashes[path.name] = hashlib.sha256(raw).hexdigest()
        loaded = yaml.safe_load(raw) or []
        if not isinstance(loaded, list):  # retrieval.yaml is a query set, not a case list
            continue
        for item in loaded:
            cases.append(GoldenCase.model_validate(item))
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"duplicate golden case ids: {dupes}")
    return cases, hashes


def all_cases(
    settings: Settings, *, suite: str = "golden"
) -> tuple[list[GoldenCase], dict[str, str]]:
    """golden | adversarial | all; the adversarial file's hash rides along under its name."""
    if suite not in ("golden", "adversarial", "all"):
        raise ValueError(f"unknown suite {suite!r}")
    cases: list[GoldenCase] = []
    hashes: dict[str, str] = {}
    if suite in ("golden", "all"):
        cases, hashes = load_cases(settings.ccp_golden_dir)
    if suite in ("adversarial", "all"):
        path = settings.ccp_adversarial_path
        if path.exists():
            hashes[f"adversarial/{path.name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
        cases = cases + load_adversarial(path)
    return cases, hashes


def select(
    cases: Iterable[GoldenCase],
    *,
    level: str | None = None,
    tag: str | None = None,
    case_id: str | None = None,
) -> list[GoldenCase]:
    chosen = list(cases)
    if level:
        chosen = [c for c in chosen if c.level == level]
    if tag:
        chosen = [c for c in chosen if tag in c.tags]
    if case_id:
        chosen = [c for c in chosen if c.id == case_id]
    return chosen


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return out.stdout.strip() or None


def missing_providers(
    settings: Settings,
    case: GoldenCase,
    *,
    judge_available: bool = False,
    providers_available: bool = False,
) -> list[str]:
    """Keys the case still needs: its own requirements (unless a Providers override is
    injected, as tests do) and the judge model for judged cases (unless a judge is injected)."""
    keys = {
        "anthropic": settings.anthropic_api_key,
        "voyage": settings.voyage_api_key,
        "openai": settings.openai_api_key,
    }
    needed = [] if providers_available else list(case.requires)
    if case.grading == "judge" and not judge_available and "anthropic" not in needed:
        needed.append("anthropic")  # the judge model
    return [name for name in needed if keys.get(name) is None]


def _failed(case: GoldenCase, error: str, *, latency_ms: float = 0.0) -> CaseResult:
    return CaseResult(
        id=case.id,
        level=case.level,
        target=case.target,
        passed=False,
        latency_ms=latency_ms,
        error=error,
        attack_class=case.attack_class,
    )


def _compact(value: Any, limit: int = 4000) -> Any:
    """The target's output as stored on the result: JSON-native, long strings truncated."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "..."
    if isinstance(value, dict):
        return {str(k): _compact(v, limit) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_compact(v, limit) for v in value]
    if isinstance(value, int | float | bool) or value is None:
        return value
    return str(value)


def _expected_fields(expected: dict[str, Any]) -> int:
    inner = expected.get("$subset", expected) if isinstance(expected, dict) else {}
    fields = inner.get("extracted_fields") if isinstance(inner, dict) else None
    if isinstance(fields, dict):
        fields = fields.get("$subset", fields)
        return len(fields) if isinstance(fields, dict) else 0
    return 0


def generating_models(settings: Settings) -> tuple[str, ...]:
    """Every model that can produce what the judge grades (A-100)."""
    return (
        settings.ccp_tier2_model,
        settings.ccp_tier3_model,
        settings.investigator_model,
        settings.policy_model,
        settings.copilot_model,
    )


def run_case(ctx: EvalContext, case: GoldenCase) -> CaseResult:
    fn = TARGETS.get(case.target)
    missing = missing_providers(
        ctx.settings,
        case,
        judge_available=ctx.judge_llm is not None,
        providers_available=ctx.providers is not None,
    )
    if missing:
        return CaseResult(
            id=case.id,
            level=case.level,
            target=case.target,
            passed=False,
            skipped=True,
            latency_ms=0.0,
            error=f"skipped: requires {', '.join(missing)} (key not set)",
            attack_class=case.attack_class,
        )
    if fn is None:
        return _failed(case, f"unknown target {case.target!r}")
    rubric: Rubric | None = None
    if case.grading == "judge":
        if not case.rubric:
            return _failed(case, "judge grading needs a rubric")
        try:
            rubric = load_rubric(case.rubric, ctx.settings.ccp_rubrics_dir)
        except (OSError, ValueError) as exc:
            return _failed(case, f"rubric: {exc}")
    t0 = time.perf_counter()
    log = steps.log_path()
    offset = log.stat().st_size if log.exists() else 0  # the log is append-only
    trace_id: str | None = None
    try:
        with steps.step(
            f"eval.{case.target}",
            layer="evals",
            inputs={
                "case_id": case.id,
                "tenant": case.tenant,
                "input": case.input,
                "requires": list(case.requires),
            },
        ) as s:
            trace_id = s.step_id
            actual = fn(ctx, case.tenant, case.input)
            s.set_output(_compact(actual))
    except Exception as exc:  # a crashing target is a failed case, not a crashed run
        result = _failed(
            case,
            f"{type(exc).__name__}: {exc}",
            latency_ms=round((time.perf_counter() - t0) * 1000, 3),
        )
        result.trace_id = trace_id
        return result
    latency_ms = round((time.perf_counter() - t0) * 1000, 3)
    mismatches = compare(case.expected, actual) if case.expected else []
    judged = None
    if rubric is not None:
        llm = ctx.judge_llm or try_build_client(ctx.settings)
        if llm is None:
            mismatches.append("judge: no judge model available")
        else:
            try:
                with steps.step(
                    "eval.judge",
                    layer="evals",
                    kind="generation",
                    inputs={"case_id": case.id, "rubric": rubric.name},
                ) as js:
                    judged = judge_consistently(
                        llm,
                        rubric,
                        case_input=case.input,
                        output=_compact(actual),
                        model=ctx.settings.ccp_judge_model,
                        tracker=BudgetTracker(RunBudget.from_settings(ctx.settings)),
                        generators=generating_models(ctx.settings),
                        runs=ctx.settings.ccp_judge_runs,
                    )
                    usage = judged.usage or {}
                    steps.note_model_call(
                        judged.model,
                        input_tokens=int(usage.get("input_tokens", 0)),
                        output_tokens=int(usage.get("output_tokens", 0)),
                        cache_read=int(usage.get("cache_read", 0)),
                        cache_write=int(usage.get("cache_write", 0)),
                    )
                    js.set_output({"overall_pass": judged.overall_pass})
            except SelfGrading as exc:
                mismatches.append(f"judge: self-grading blocked: {exc}")
            except Exception as exc:
                mismatches.append(f"judge: {type(exc).__name__}: {exc}")
            else:
                if not judged.overall_pass:
                    unmet = ", ".join(c.name for c in judged.criteria if not c.met) or "overall"
                    mismatches.append(f"judge: {unmet} not met")
    tokens, usd = metrics_mod.usage_since(log, offset)
    attack: bool | None = None
    if case.attack_class is not None and isinstance(actual, dict):
        # an attack whose poisoned chunk the model never saw was not attempted
        shown = actual.get("poisoned_chunk_shown")
        attack = None if shown is False else bool(actual.get("attack_succeeded"))
    return CaseResult(
        id=case.id,
        level=case.level,
        target=case.target,
        passed=not mismatches,
        mismatches=mismatches,
        latency_ms=latency_ms,
        judge=judged,
        actual=_compact(actual) if isinstance(actual, dict) else {"value": _compact(actual)},
        expected_fields=_expected_fields(case.expected),
        attack_class=case.attack_class,
        attack_succeeded=attack,
        cost_usd=usd,
        tokens=tokens,
        trace_id=trace_id,
    )


def _run_in_fresh_database(
    settings: Settings, case: GoldenCase, judge_llm: LLMClient | None
) -> CaseResult:
    with scratch_database(settings, prefix="ccp_evalcase") as db_settings:
        ctx = EvalContext(settings=settings, db_settings=db_settings, judge_llm=judge_llm)
        try:
            return run_case(ctx, case)
        finally:
            ctx.close()


def run_unit_tests(repo_root: Path = REPO_ROOT) -> LevelSummary:
    """The unit row: ordinary pytest over tests/unit, counted like a level."""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit", "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
    )
    return parse_pytest_summary(out.stdout, out.returncode)


def parse_pytest_summary(stdout: str, returncode: int) -> LevelSummary:
    """The unit row from pytest's footer; a run that exits non-zero without a parsable
    footer (a collection crash, a missing pytest) counts as one failure, never as 0/0."""
    counts = {kind: int(n) for n, kind in _PYTEST_COUNT.findall(stdout)}
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    if returncode != 0 and failed == 0:
        failed = 1
    return LevelSummary(passed=passed, total=passed + failed)


def compute_metrics(results: list[CaseResult], settings: Settings) -> RunMetrics:
    bins, ece, count = metrics_mod.reliability(results)
    asr, counts = metrics_mod.attack_success(results)
    latency = metrics_mod.latency_by_target(results, metrics_mod.load_slos(settings.ccp_slos_path))
    cost = metrics_mod.cost_by_target(results)
    return RunMetrics(
        reliability=bins,
        ece=ece,
        ece_count=count,
        suggested_interrupt_threshold=metrics_mod.suggested_threshold(bins),
        asr=asr,
        asr_counts=counts,
        latency=latency,
        cost_usd=cost,
        total_cost_usd=round(sum(cost.values()), 6),
        extraction=metrics_mod.extraction_summary(results),
        judge=metrics_mod.judge_summary(results),
    )


def run(
    settings: Settings,
    cases: list[GoldenCase],
    golden_hashes: dict[str, str],
    *,
    judge_llm: LLMClient | None = None,
    unit: bool = False,
) -> RunReport:
    started = datetime.now(UTC)
    run_id = steps.new_run()  # one run id for the report, the step log, and the trace
    judge_llm = judge_llm or try_build_client(settings)
    needs_db = any(c.target in DB_TARGETS for c in cases)
    results: list[CaseResult] = []
    if needs_db:
        try:
            with scratch_database(settings, prefix="ccp_eval") as db_settings:
                ctx = EvalContext(settings=settings, db_settings=db_settings, judge_llm=judge_llm)
                try:
                    results = [
                        _run_in_fresh_database(settings, c, judge_llm)
                        if c.target in FRESH_DB_TARGETS
                        else run_case(ctx, c)
                        for c in cases
                    ]
                finally:
                    ctx.close()
        except psycopg.OperationalError as exc:
            ctx = EvalContext(settings=settings, judge_llm=judge_llm)
            results = [
                run_case(ctx, c)
                if c.target not in DB_TARGETS
                else _failed(c, f"database unavailable: {exc}")
                for c in cases
            ]
    else:
        ctx = EvalContext(settings=settings, judge_llm=judge_llm)
        results = [run_case(ctx, c) for c in cases]
    summary: dict[str, LevelSummary] = {}
    for level in LEVELS:
        at_level = [r for r in results if r.level == level]
        summary[level] = LevelSummary(
            passed=sum(r.passed for r in at_level),
            total=len(at_level),
            skipped=sum(r.skipped for r in at_level),
        )
    return RunReport(
        run_id=run_id,
        started_at=started,
        finished_at=datetime.now(UTC),
        git_commit=_git_commit(),
        golden_sha256=golden_hashes,
        bundle_sha256=bundle_sha256(),
        cases=results,
        summary=summary,
        unit=run_unit_tests() if unit else None,
        metrics=compute_metrics(results, settings),
        models={
            "tier2": settings.ccp_tier2_model,
            "tier3": settings.ccp_tier3_model,
            "judge": settings.ccp_judge_model,
            "embed": settings.ccp_embed_model,
        },
    )


def write_report(report: RunReport, runs_dir: Path, *, promote: bool = True) -> Path:
    """Write the run under its id; promote it to latest.json (the regression baseline)
    unless the caller says the run must not become the baseline."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{report.run_id}.json"
    payload = report.model_dump(mode="json")
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if promote:
        (runs_dir / "latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def write_threshold(path: Path, value: float, *, on: datetime | None = None) -> Path:
    """Record an interrupt threshold read off the reliability curve (config/evaluation.yaml)."""
    stamp = (on or datetime.now(UTC)).date().isoformat()
    path.write_text(
        "# Human-interrupt threshold for the AI tiers (spec.md A-10, A-47). Read off the\n"
        "# reliability curve by `ccp evals threshold --apply`; `read_from_curve_on` records when.\n"
        f"interrupt_threshold: {value:.2f}\n"
        f'read_from_curve_on: "{stamp}"\n',
        encoding="utf-8",
    )
    return path


def format_metrics(metrics: RunMetrics) -> str:
    lines: list[str] = []
    if metrics.ece is not None:
        lines.append(
            f"reliability: ECE {metrics.ece:.3f} over {metrics.ece_count} verdict cases in"
            f" {len(metrics.reliability)} bins"
        )
        for b in metrics.reliability:
            lines.append(
                f"    [{b.lower:.1f}, {b.upper:.1f}) n={b.count:<3} confidence"
                f" {b.mean_confidence:.2f} accuracy {b.accuracy:.2f}"
            )
        suggested = metrics.suggested_interrupt_threshold
        lines.append(
            "    suggested interrupt threshold: "
            + (f"{suggested:.2f}" if suggested is not None else "too few points to read one")
        )
    else:
        lines.append("reliability: no verdict cases with a confidence ran (keyless run)")
    if metrics.asr_counts:
        parts = [f"{k} {v[0]}/{v[1]}" for k, v in metrics.asr_counts.items() if k != "overall"]
        overall = metrics.asr_counts.get("overall", [0, 0])
        lines.append(
            f"attack success rate: {overall[0]}/{overall[1]} overall"
            f" ({metrics.asr.get('overall', 0.0):.0%}); by class: {', '.join(parts)}"
        )
    else:
        lines.append("attack success rate: no adversarial cases ran")
    if metrics.latency:
        lines.append("latency ms per target (p50 / p95 / p99; SLO p95):")
        for target, row in metrics.latency.items():
            slo = f"{row.slo_p95_ms:.0f}" if row.slo_p95_ms is not None else "none"
            flag = "  BREACH" if row.breached else ""
            lines.append(
                f"    {target:<22} {row.p50:>9.1f} {row.p95:>9.1f} {row.p99:>9.1f}"
                f"  (SLO {slo}) n={row.count}{flag}"
            )
    if metrics.cost_usd:
        parts = [f"{k} {v:.4f}" for k, v in metrics.cost_usd.items()]
        lines.append(
            f"cost USD per target: {', '.join(parts)} (total {metrics.total_cost_usd:.4f})"
        )
    else:
        lines.append("cost: no model calls charged (keyless run)")
    if metrics.extraction:
        for schema, row in metrics.extraction.items():
            acc = row.get("field_accuracy")
            prec = row.get("support_precision")
            lines.append(
                f"extraction {schema}: field accuracy"
                f" {acc if acc is None else f'{acc:.2f}'}, support precision"
                f" {prec if prec is None else f'{prec:.2f}'} ({row.get('cases')} cases)"
            )
    if metrics.judge.get("cases"):
        lines.append(
            f"judge: {metrics.judge['overall_pass']}/{metrics.judge['cases']} judged cases passed"
        )
    return "\n".join(lines)


def format_table(report: RunReport) -> str:
    lines = [f"{'case':<44} {'level':<10} {'result':<7} {'ms':>8}"]
    for r in report.cases:
        verdict = "SKIP" if r.skipped else "PASS" if r.passed else "FAIL"
        lines.append(f"{r.id:<44} {r.level:<10} {verdict:<7} {r.latency_ms:>8.1f}")
        for m in r.mismatches:
            lines.append(f"    - {m}")
        if r.error:
            lines.append(f"    ! {r.error}")
    lines.append("")
    if report.unit is not None:
        lines.append(
            f"{'unit':<10} {report.unit.passed}/{report.unit.total} passed (pytest tests/unit)"
        )
    else:
        lines.append("unit       pytest tests/unit (not run; pass --unit to include the row)")
    for level in LEVELS:
        s = report.summary[level]
        skipped = f", {s.skipped} skipped (no key)" if s.skipped else ""
        lines.append(f"{level:<10} {s.passed}/{s.total - s.skipped} passed{skipped}")
    if report.metrics is not None:
        lines.append("")
        lines.append(format_metrics(report.metrics))
    return "\n".join(lines)
