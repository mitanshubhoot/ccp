"""Production quality jobs (spec.md A-144), scheduled like any job (A-108).

    canary          daily: the canary cases (config/canary.yaml), golden cases that call the
                    real model, run against the bundle in production; a pass rate below the
                    floor fails the job, so a provider-side change shows within a day
    judged_sample   nightly: up to 20 AI verdicts from the last day, judged against the
                    verdict_reasoning rubric (does the reasoning follow from the checked
                    quotes); recorded only as advisory unless the judge is calibrated (A-144)

Both record a quality check (kind, bundle, what was measured, passed). Without a model key
neither can run: each records nothing and says so, rather than reporting a pass.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from ulid import ULID

from ccp.config import REPO_ROOT, Settings
from ccp.evals import judge as judging
from ccp.evals import regress
from ccp.evals import runner as eval_runner
from ccp.evaluators import jobs
from ccp.evaluators import verdict as verdicts
from ccp.llm import release
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import try_build_client
from ccp.storage.scoped import ScopedDB

CANARY_FILE = REPO_ROOT / "config" / "canary.yaml"
SAMPLE = 20


def _record(
    db: ScopedDB, settings: Settings, kind: str, passed: bool, measured: dict[str, Any]
) -> None:
    db.execute(
        "INSERT INTO quality_checks (tenant_id, check_id, kind, bundle_id, passed, measured)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            str(ULID()),
            kind,
            release.bundle_id(settings),
            passed,
            json.dumps(measured, default=str),
        ),
    )


def canary_ids(path: Path = CANARY_FILE) -> tuple[list[str], float]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    loaded = loaded or {}
    return [str(c) for c in loaded.get("cases") or []], float(loaded.get("floor", 1.0))


def canary(db: ScopedDB, settings: Settings, slot: datetime) -> int:
    if try_build_client(settings) is None:
        return 0  # no key: nothing ran, and nothing is reported as passing
    ids, floor = canary_ids()
    cases, hashes = eval_runner.load_cases(settings.ccp_golden_dir)
    chosen = [c for c in cases if c.id in set(ids)]
    report = eval_runner.run(settings, chosen, hashes, runs=1)
    ran = [r for r in report.cases if not r.skipped]
    rate = sum(1 for r in ran if r.passed) / len(ran) if ran else 0.0
    passed = bool(ran) and rate >= floor
    _record(
        db,
        settings,
        "canary",
        passed,
        {
            "cases": len(ran),
            "pass_rate": rate,
            "floor": floor,
            "failed": [r.id for r in ran if not r.passed],
        },
    )
    if not passed:
        raise RuntimeError(f"canary below its floor: {rate:.0%} < {floor:.0%}")
    return len(ran)


def judged_sample(db: ScopedDB, settings: Settings, slot: datetime) -> int:
    llm = try_build_client(settings)
    if llm is None:
        return 0
    since = slot - timedelta(days=1)
    ids = db.fetch_all(
        "SELECT verdict_id FROM verdicts WHERE tier > 1 AND created_at >= %s"
        " ORDER BY verdict_id DESC LIMIT %s",
        (since, SAMPLE),
    )
    rubric = judging.load_rubric("verdict_reasoning")
    scores: list[dict[str, Any]] = []
    for (verdict_id,) in ids:
        found = verdicts.load(db, str(verdict_id))
        if found is None:
            continue
        result = judging.judge(
            llm,
            rubric,
            case_input={"test_id": found.test_id, "subject": found.subject_key},
            output={
                "status": found.status.value,
                "reasoning": found.reasoning,
                "citations": [c.model_dump() for c in found.citations if c.verified],
            },
            model=settings.ccp_judge_model,
            tracker=BudgetTracker(RunBudget.from_settings(settings)),
        )
        scores.append({"verdict_id": found.verdict_id, "passed": result.overall_pass})
    agreement = regress.latest_judge_agreement(settings.ccp_eval_runs_dir)
    trusted = agreement is not None and agreement >= regress.load_gates().judge_min_agreement
    rate = sum(1 for s in scores if s["passed"]) / len(scores) if scores else 1.0
    _record(
        db,
        settings,
        "judged_sample",
        rate >= 0.9 or not trusted,  # an uncalibrated judge reports, never fails the job
        {
            "judged": len(scores),
            "pass_rate": rate,
            "judge_trusted": trusted,
            "judge_agreement": agreement,
            "scores": scores,
        },
    )
    return len(scores)


jobs.JOBS["canary"] = canary
jobs.JOBS["judged_sample"] = judged_sample
