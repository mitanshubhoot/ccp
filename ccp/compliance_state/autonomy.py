"""Which drafts skip a person's review, and on what evidence (spec.md A-171; LT8 narrowed
to review, since CCP suggests and changes nothing in a customer's systems).

Every (tenant, workflow, decision class) has a level: shadow, suggest, approve_each (the
default) or sampled. Only a class config/autonomy.yaml marks reversible and low impact may
be promoted to sampled, only by an administrator, and only on a published quality snapshot
(A-170) for its workflow that was measured on the release bundle in force and meets the
bar: a false-accept upper bound, the FAIL-labelled cases it rests on, and pass^k. At
sampled, a draft is accepted without review unless a seeded sample or the day's exposure
budget sends it to a person; every decision is recorded citing the snapshot. A watch
demotes the class to approve_each when the bundle changes or a quality check fails. A
simulator replays past drafts with their human outcomes through a candidate level.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from ulid import ULID

from ccp.config import REPO_ROOT, Settings
from ccp.evaluators import jobs
from ccp.llm import release
from ccp.storage.scoped import ScopedDB

POLICY_FILE = REPO_ROOT / "config" / "autonomy.yaml"
PUBLISHED_DIR = REPO_ROOT / "evals" / "published"
Level = Literal["shadow", "suggest", "approve_each", "sampled"]
ALARMS = ("relabel", "adequacy", "canary", "judged_sample")


@lru_cache(maxsize=2)
def policy(path: Path = POLICY_FILE) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return raw


def class_rules(workflow: str, decision_class: str) -> dict[str, Any]:
    found = (policy().get("classes") or {}).get(workflow, {}).get(decision_class)
    if found is None:
        raise ValueError(f"no decision class {workflow}/{decision_class} in autonomy.yaml")
    return dict(found)


def latest_snapshot(workflow: str, out_dir: Path = PUBLISHED_DIR) -> dict[str, Any] | None:
    found = sorted(
        out_dir.glob(f"*/{workflow}-*.json"), key=lambda p: (p.parent.name, p.stat().st_mtime)
    )
    return json.loads(found[-1].read_text(encoding="utf-8")) if found else None


@dataclass(frozen=True)
class Current:
    level_id: str
    level: Level
    snapshot: dict[str, Any] | None
    bundle_id: str | None


def current(db: ScopedDB, workflow: str, decision_class: str) -> Current:
    row = db.fetch_one(
        "SELECT level_id, level, snapshot, bundle_id FROM autonomy_levels"
        " WHERE workflow = %s AND decision_class = %s ORDER BY set_at DESC, level_id DESC LIMIT 1",
        (workflow, decision_class),
    )
    if row is None:
        return Current("default", "approve_each", None, None)
    snapshot = json.loads(row[2]) if isinstance(row[2], str) else row[2]
    return Current(str(row[0]), row[1], snapshot, row[3])


def _record(
    db: ScopedDB,
    workflow: str,
    decision_class: str,
    level: Level,
    by: str,
    reason: str,
    snapshot: dict[str, Any] | None = None,
    bundle_id: str | None = None,
) -> str:
    level_id = str(ULID())
    db.execute(
        "INSERT INTO autonomy_levels (tenant_id, level_id, workflow, decision_class, level,"
        " snapshot, bundle_id, set_by, reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            level_id,
            workflow,
            decision_class,
            level,
            json.dumps(snapshot) if snapshot is not None else None,
            bundle_id,
            by,
            reason,
        ),
    )
    return level_id


def promotion_problems(
    rules: dict[str, Any], snapshot: dict[str, Any] | None, bundle: str
) -> list[str]:
    bar = policy().get("promotion") or {}
    problems: list[str] = []
    if not (rules.get("reversible") and rules.get("impact") == "low"):
        problems.append("only a reversible, low-impact class may skip review")
    if snapshot is None:
        return [*problems, "no published quality snapshot for the workflow"]
    if snapshot.get("bundle_id") != bundle:
        problems.append(f"the snapshot measured bundle {snapshot.get('bundle_id')}, not {bundle}")
    upper = snapshot.get("false_pass_upper")
    if upper is None:
        problems.append(f"the snapshot withholds its bound: {snapshot.get('withheld')}")
    elif upper > float(bar.get("max_false_accept_upper", 0.01)):
        problems.append(
            f"false-accept upper bound {upper} over {bar.get('max_false_accept_upper')}"
        )
    if int(snapshot.get("fail_labelled") or 0) < int(bar.get("min_fail_cases", 300)):
        cases, needed = snapshot.get("fail_labelled"), bar.get("min_fail_cases")
        problems.append(f"{cases} FAIL-labelled cases; at least {needed}")
    if float(snapshot.get("pass_k") or 0) < float(bar.get("min_pass_k", 0.9)):
        problems.append(f"pass^k {snapshot.get('pass_k')} under {bar.get('min_pass_k')}")
    return problems


def set_level(
    db: ScopedDB,
    settings: Settings,
    workflow: str,
    decision_class: str,
    level: Level,
    by: str,
    reason: str,
    snapshot_dir: Path = PUBLISHED_DIR,
) -> str:
    """An administrator's choice of level. Sampled is refused unless every bar is met."""
    if not by.strip():
        raise ValueError("a level is set by a named administrator")
    rules = class_rules(workflow, decision_class)
    if level != "sampled":
        return _record(db, workflow, decision_class, level, by, reason)
    bundle = release.bundle_id(settings)
    snapshot = latest_snapshot(str(rules.get("snapshot") or workflow), snapshot_dir)
    problems = promotion_problems(rules, snapshot, bundle)
    if problems:
        raise ValueError("; ".join(problems))
    return _record(db, workflow, decision_class, "sampled", by, reason, snapshot, bundle)


def _sampled(tenant_id: str, workflow: str, decision_class: str, item_id: str, rate: float) -> bool:
    digest = hashlib.sha256(
        f"{tenant_id}:{workflow}:{decision_class}:{item_id}".encode()
    ).hexdigest()
    return int(digest[:8], 16) % 1000 < int(rate * 1000)


def decide(
    db: ScopedDB, workflow: str, decision_class: str, item_id: str, now: datetime | None = None
) -> str:
    """review (a person reads it), or at sampled: auto, sampled_for_review or over_budget,
    each recorded citing the snapshot."""
    found = current(db, workflow, decision_class)
    if found.level != "sampled" or found.snapshot is None:
        return "review"
    exposure = policy().get("exposure") or {}
    moment = now or datetime.now(UTC)
    used = db.fetch_one(
        "SELECT count(*) FROM auto_decisions WHERE workflow = %s AND decision_class = %s"
        " AND outcome = 'auto' AND decided_at >= %s",
        (workflow, decision_class, moment - timedelta(days=1)),
    )
    if used is not None and int(used[0]) >= int(exposure.get("max_auto_per_day", 50)):
        outcome = "over_budget"
    elif _sampled(
        db.tenant.tenant_id,
        workflow,
        decision_class,
        item_id,
        float(exposure.get("sample_rate", 0.1)),
    ):
        outcome = "sampled_for_review"
    else:
        outcome = "auto"
    db.execute(
        "INSERT INTO auto_decisions (tenant_id, decision_id, workflow, decision_class, item_id,"
        " outcome, level_id, snapshot) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            str(ULID()),
            workflow,
            decision_class,
            item_id,
            outcome,
            found.level_id,
            json.dumps(found.snapshot),
        ),
    )
    return outcome


def watch(db: ScopedDB, settings: Settings) -> list[str]:
    """Demote every sampled class whose bundle changed or whose quality alarm fired."""
    bundle = release.bundle_id(settings)
    alarm = db.fetch_one(
        "SELECT kind FROM quality_checks WHERE NOT passed AND kind = ANY(%s)"
        " AND recorded_at >= clock_timestamp() - interval '1 day' LIMIT 1",
        (list(ALARMS),),
    )
    demoted: list[str] = []
    for workflow, classes in (policy().get("classes") or {}).items():
        for decision_class in classes:
            found = current(db, workflow, decision_class)
            if found.level != "sampled":
                continue
            why = (
                f"bundle changed from {found.bundle_id} to {bundle}"
                if found.bundle_id != bundle
                else f"quality check {alarm[0]} failed"
                if alarm
                else None
            )
            if why:
                _record(db, workflow, decision_class, "approve_each", "ccp", f"demoted: {why}")
                demoted.append(f"{workflow}/{decision_class}: {why}")
    return demoted


@dataclass(frozen=True)
class Simulation:
    items: int
    auto: int
    sampled_for_review: int
    over_budget: int
    false_accepts: int  # auto-accepted, but a person had changed or rejected it


def simulate(
    tenant_id: str,
    workflow: str,
    decision_class: str,
    history: list[tuple[str, datetime, Literal["accepted", "changed", "rejected"]]],
) -> Simulation:
    """Replay past drafts, with what a person did to each, through sampled: how many would
    have skipped review, and how many of those a person had in fact changed or rejected."""
    exposure = policy().get("exposure") or {}
    rate = float(exposure.get("sample_rate", 0.1))
    budget = int(exposure.get("max_auto_per_day", 50))
    per_day: dict[str, int] = {}
    auto = sampled = over = false = 0
    for item_id, at, outcome in sorted(history, key=lambda h: h[1]):
        day = at.date().isoformat()
        if per_day.get(day, 0) >= budget:
            over += 1
        elif _sampled(tenant_id, workflow, decision_class, item_id, rate):
            sampled += 1
        else:
            auto += 1
            per_day[day] = per_day.get(day, 0) + 1
            false += outcome != "accepted"
    return Simulation(len(history), auto, sampled, over, false)


def questionnaire_history(
    db: ScopedDB, source: str = "library"
) -> list[tuple[str, datetime, Literal["accepted", "changed", "rejected"]]]:
    """Past questionnaire drafts of one source and what a person did to each."""
    rows = db.fetch_all(
        "SELECT a.answer_id, a.created_at, bool_or(e.status = 'rejected'),"
        " bool_or(e.status = 'approved' AND e.edited_text IS NOT NULL),"
        " bool_or(e.status = 'approved')"
        " FROM questionnaire_answers a JOIN answer_events e"
        " ON e.tenant_id = a.tenant_id AND e.answer_id = a.answer_id"
        " WHERE a.source = %s GROUP BY a.answer_id, a.created_at",
        (source,),
    )
    out: list[tuple[str, datetime, Literal["accepted", "changed", "rejected"]]] = []
    for answer_id, created, rejected, edited, approved in rows:
        if rejected:
            out.append((str(answer_id), created, "rejected"))
        elif edited:
            out.append((str(answer_id), created, "changed"))
        elif approved:
            out.append((str(answer_id), created, "accepted"))
    return out


jobs.JOBS["autonomy_watch"] = lambda db, settings, slot: len(watch(db, settings))
