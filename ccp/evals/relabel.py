"""Stratified judge samples and the monthly expert relabel (spec.md A-157, report 6.9
"Sampling, cadence and cost" and "Judge drift and re-calibration").

    stratify(candidates)   the nightly sample: strata of verdict status, confidence band
                           (near the review threshold, above, below) and evidence type; each
                           round gives every stratum one draw and AI-derived PASS results and
                           near-threshold ones extra (twice each, four times for both), so a
                           rare stratum is never left out and the risky ones weigh more. The
                           draw is seeded by the night, so a rerun picks the same items
    monthly_batch(db)      on the 1st, 20 of last month's judged items for an expert: the
                           ones the judge disagreed with first, then a spread over the strata
    label(db, item)        an expert's label on one of them
    check(db, month)       the month's agreement between expert and judge, against the judge's
                           calibrated interval (`ccp evals calibrate`); outside it, the check
                           fails and says to re-calibrate. Fewer than 10 labels decides nothing

People do the labelling; this module only chooses what they look at and counts what they said.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ulid import ULID

from ccp.evaluators import jobs
from ccp.storage.scoped import ScopedDB

NEAR_BAND = 0.10  # within this of the review threshold is "near"
RELABEL_SIZE = 20
MIN_LABELS = 10


@dataclass(frozen=True)
class Candidate:
    verdict_id: str
    test_id: str
    status: str
    confidence: float | None
    evidence_type: str


def band(confidence: float | None, threshold: float) -> str:
    if confidence is None:
        return "unknown"
    if abs(confidence - threshold) <= NEAR_BAND:
        return "near"
    return "high" if confidence > threshold else "low"


def stratum(c: Candidate, threshold: float) -> str:
    return f"{c.status}|{band(c.confidence, threshold)}|{c.evidence_type}"


def weight(key: str) -> int:
    status, near, _ = key.split("|", 2)
    return (2 if status == "pass" else 1) * (2 if near == "near" else 1)


def stratify(
    candidates: list[Candidate], size: int, *, threshold: float, seed: str
) -> list[tuple[str, Candidate]]:
    """(stratum, candidate) pairs, at most `size`, drawn round by round."""
    rng = random.Random(seed)  # noqa: S311 - sampling, not secrecy
    groups: dict[str, list[Candidate]] = {}
    for c in sorted(candidates, key=lambda c: c.verdict_id):
        groups.setdefault(stratum(c, threshold), []).append(c)
    for members in groups.values():
        rng.shuffle(members)
    order = sorted(groups, key=lambda k: (-weight(k), k))
    picked: list[tuple[str, Candidate]] = []
    while len(picked) < size and any(groups.values()):
        for key in order:
            for _ in range(weight(key)):
                if groups[key] and len(picked) < size:
                    picked.append((key, groups[key].pop()))
    return picked


def record_judged(
    db: ScopedDB,
    check_id: str,
    key: str,
    c: Candidate,
    judge_pass: bool,
    judge_model: str,
) -> str:
    item_id = str(ULID())
    db.execute(
        "INSERT INTO judged_items (tenant_id, item_id, check_id, verdict_id, test_id, stratum,"
        " status, confidence, judge_pass, judge_model) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,"
        " %s, %s)",
        (
            db.tenant.tenant_id,
            item_id,
            check_id,
            c.verdict_id,
            c.test_id,
            key,
            c.status,
            c.confidence,
            judge_pass,
            judge_model,
        ),
    )
    return item_id


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    year, mon = (int(x) for x in month.split("-"))
    start = datetime(year, mon, 1, tzinfo=UTC)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=UTC)
    return start, end


def previous_month(moment: datetime) -> str:
    year, mon = moment.year, moment.month - 1
    if mon == 0:
        year, mon = year - 1, 12
    return f"{year:04d}-{mon:02d}"


def monthly_batch(db: ScopedDB, slot: datetime) -> int:
    """On the 1st: last month's batch, once. Disagreements first, then the strata in turn."""
    if slot.day != 1:
        return 0
    month = previous_month(slot)
    if db.fetch_one("SELECT 1 FROM relabel_batches WHERE month = %s", (month,)):
        return 0
    start, end = _month_bounds(month)
    rows = db.fetch_all(
        "SELECT item_id, stratum, judge_pass FROM judged_items"
        " WHERE judged_at >= %s AND judged_at < %s ORDER BY item_id",
        (start, end),
    )
    if not rows:
        return 0
    rng = random.Random(f"{db.tenant.tenant_id}:{month}")  # noqa: S311 - sampling
    disagreements = [str(r[0]) for r in rows if not r[2]]
    rng.shuffle(disagreements)
    by_stratum: dict[str, list[str]] = {}
    for item_id, key, judge_pass in rows:
        if judge_pass:
            by_stratum.setdefault(str(key), []).append(str(item_id))
    for members in by_stratum.values():
        rng.shuffle(members)
    chosen = disagreements[:RELABEL_SIZE]
    while len(chosen) < RELABEL_SIZE and any(by_stratum.values()):
        for key in sorted(by_stratum):
            if by_stratum[key] and len(chosen) < RELABEL_SIZE:
                chosen.append(by_stratum[key].pop())
    db.execute(
        "INSERT INTO relabel_batches (tenant_id, batch_id, month, item_ids)"
        " VALUES (%s, %s, %s, %s)",
        (db.tenant.tenant_id, str(ULID()), month, json.dumps(chosen)),
    )
    return len(chosen)


def batch(db: ScopedDB, month: str) -> list[dict[str, Any]]:
    """The month's items with their verdicts, the judge's call, and any expert label."""
    row = db.fetch_one("SELECT item_ids FROM relabel_batches WHERE month = %s", (month,))
    if row is None:
        return []
    ids = row[0] if isinstance(row[0], list) else json.loads(row[0])
    out: list[dict[str, Any]] = []
    for item_id in ids:
        found = db.fetch_one(
            "SELECT j.verdict_id, j.test_id, j.stratum, j.judge_pass,"
            " (SELECT label_pass FROM expert_labels e WHERE e.item_id = j.item_id"
            "  ORDER BY labelled_at DESC LIMIT 1)"
            " FROM judged_items j WHERE j.item_id = %s",
            (item_id,),
        )
        if found is not None:
            out.append(
                {
                    "item_id": item_id,
                    "verdict_id": found[0],
                    "test_id": found[1],
                    "stratum": found[2],
                    "judge_pass": found[3],
                    "expert_pass": found[4],
                }
            )
    return out


def label(db: ScopedDB, item_id: str, *, passed: bool, by: str, note: str = "") -> str:
    if db.fetch_one("SELECT 1 FROM judged_items WHERE item_id = %s", (item_id,)) is None:
        raise ValueError(f"no judged item {item_id!r}")
    label_id = str(ULID())
    db.execute(
        "INSERT INTO expert_labels (tenant_id, label_id, item_id, label_pass, annotator, note)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (db.tenant.tenant_id, label_id, item_id, passed, by, note),
    )
    return label_id


def latest_calibration(runs_dir: Path) -> dict[str, Any] | None:
    """The overall row of the newest judge calibration: agreement and its interval."""
    reports = sorted(runs_dir.glob("calibration-*.json"))
    if not reports:
        return None
    overall = json.loads(reports[-1].read_text(encoding="utf-8")).get("overall") or {}
    return dict(overall) if overall else None


def check(db: ScopedDB, month: str, runs_dir: Path, bundle_id: str) -> dict[str, Any]:
    """The month's expert-judge agreement against the calibrated interval; recorded as a
    quality check, which fails (and asks for re-calibration) when it falls outside."""
    items = [i for i in batch(db, month) if i["expert_pass"] is not None]
    calibrated = latest_calibration(runs_dir)
    out: dict[str, Any] = {"month": month, "labelled": len(items), "calibration": calibrated}
    if len(items) < MIN_LABELS:
        out["decided"] = False
        return out
    agreement = sum(1 for i in items if i["expert_pass"] == i["judge_pass"]) / len(items)
    out["agreement"] = round(agreement, 4)
    if calibrated is None:
        inside = False  # an uncalibrated judge cannot be inside its interval
    else:
        inside = float(calibrated["ci_low"]) <= agreement <= float(calibrated["ci_high"])
    out.update(decided=True, inside_interval=inside, recalibrate=not inside)
    db.execute(
        "INSERT INTO quality_checks (tenant_id, check_id, kind, bundle_id, passed, measured)"
        " VALUES (%s, %s, 'relabel', %s, %s, %s)",
        (db.tenant.tenant_id, str(ULID()), bundle_id, inside, json.dumps(out, default=str)),
    )
    return out


jobs.JOBS["relabel"] = lambda db, settings, slot: monthly_batch(db, slot)
