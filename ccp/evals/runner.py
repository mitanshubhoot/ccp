"""The eval runner: load golden cases, run each target, compare, write a report.

    ccp evals run [--level component] [--tag t] [--id case-id]

Reports land in evals/runs/{run_id}.json and evals/runs/latest.json. Cases needing a
database run against one scratch database created for the run.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import yaml
from ulid import ULID

from ccp.config import Settings
from ccp.evals.compare import compare
from ccp.evals.schema import CaseResult, GoldenCase, LevelSummary, RunReport
from ccp.evals.targets import TARGETS, EvalContext
from ccp.storage.db import scratch_database

DB_TARGETS = {
    "ingest_land",
    "ingest_ordering",
    "deterministic_verdict",
    "evaluate_tier1",
    "chunker_pages",
    "retrieval_metrics",
    "tier2_verdict",
}
# Targets whose expectations describe a fresh database (first landing, no prior rows) get a
# scratch database of their own; every other database case shares the run's database.
FRESH_DB_TARGETS = {"ingest_land"}


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


def missing_providers(settings: Settings, case: GoldenCase) -> list[str]:
    keys = {
        "anthropic": settings.anthropic_api_key,
        "voyage": settings.voyage_api_key,
        "openai": settings.openai_api_key,
    }
    return [name for name in case.requires if keys.get(name) is None]


def run_case(ctx: EvalContext, case: GoldenCase) -> CaseResult:
    fn = TARGETS.get(case.target)
    t0 = time.perf_counter()
    missing = missing_providers(ctx.settings, case)
    if missing:
        return CaseResult(
            id=case.id,
            level=case.level,
            target=case.target,
            passed=False,
            skipped=True,
            latency_ms=0.0,
            error=f"skipped: requires {', '.join(missing)} (key not set)",
        )
    if fn is None:
        return CaseResult(
            id=case.id,
            level=case.level,
            target=case.target,
            passed=False,
            latency_ms=0.0,
            error=f"unknown target {case.target!r}",
        )
    if case.grading != "exact":
        return CaseResult(
            id=case.id,
            level=case.level,
            target=case.target,
            passed=False,
            latency_ms=0.0,
            error="judge grading arrives in Phase 15",
        )
    try:
        actual = fn(ctx, case.tenant, case.input)
    except Exception as exc:  # a crashing target is a failed case, not a crashed run
        return CaseResult(
            id=case.id,
            level=case.level,
            target=case.target,
            passed=False,
            latency_ms=round((time.perf_counter() - t0) * 1000, 3),
            error=f"{type(exc).__name__}: {exc}",
        )
    mismatches = compare(case.expected, actual)
    return CaseResult(
        id=case.id,
        level=case.level,
        target=case.target,
        passed=not mismatches,
        mismatches=mismatches,
        latency_ms=round((time.perf_counter() - t0) * 1000, 3),
    )


def _run_in_fresh_database(settings: Settings, case: GoldenCase) -> CaseResult:
    with scratch_database(settings, prefix="ccp_evalcase") as db_settings:
        ctx = EvalContext(settings=settings, db_settings=db_settings)
        try:
            return run_case(ctx, case)
        finally:
            ctx.close()


def run(settings: Settings, cases: list[GoldenCase], golden_hashes: dict[str, str]) -> RunReport:
    started = datetime.now(UTC)
    needs_db = any(c.target in DB_TARGETS for c in cases)
    results: list[CaseResult] = []
    if needs_db:
        try:
            with scratch_database(settings, prefix="ccp_eval") as db_settings:
                ctx = EvalContext(settings=settings, db_settings=db_settings)
                try:
                    results = [
                        _run_in_fresh_database(settings, c)
                        if c.target in FRESH_DB_TARGETS
                        else run_case(ctx, c)
                        for c in cases
                    ]
                finally:
                    ctx.close()
        except psycopg.OperationalError as exc:
            ctx = EvalContext(settings=settings)
            results = [
                run_case(ctx, c)
                if c.target not in DB_TARGETS
                else CaseResult(
                    id=c.id,
                    level=c.level,
                    target=c.target,
                    passed=False,
                    latency_ms=0.0,
                    error=f"database unavailable: {exc}",
                )
                for c in cases
            ]
    else:
        ctx = EvalContext(settings=settings)
        results = [run_case(ctx, c) for c in cases]
    summary: dict[str, LevelSummary] = {}
    for level in ("component", "workflow", "e2e"):
        at_level = [r for r in results if r.level == level]
        summary[level] = LevelSummary(
            passed=sum(r.passed for r in at_level),
            total=len(at_level),
            skipped=sum(r.skipped for r in at_level),
        )
    return RunReport(
        run_id=str(ULID()),
        started_at=started,
        finished_at=datetime.now(UTC),
        git_commit=_git_commit(),
        golden_sha256=golden_hashes,
        cases=results,
        summary=summary,
    )


def write_report(report: RunReport, runs_dir: Path) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{report.run_id}.json"
    payload = report.model_dump(mode="json")
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (runs_dir / "latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


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
    lines.append("unit       pytest tests/unit (run separately; not golden-cased)")
    for level in ("component", "workflow", "e2e"):
        s = report.summary[level]
        skipped = f", {s.skipped} skipped (no key)" if s.skipped else ""
        lines.append(f"{level:<10} {s.passed}/{s.total - s.skipped} passed{skipped}")
    return "\n".join(lines)
