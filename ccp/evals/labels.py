"""Reviewer decisions as intake for the labelled set (spec.md A-100, A-103).

The labelled set grows from real reviews. ``ccp evals labels --tenant T --out PATH`` writes
every review label the tenant consented to eval use for (consent scope eval or training) on
a tier 3 draft verdict as a case in the labelled-set format: the draft as the output, the
reviewer's decision as the overall label (approve: the draft was right; override: it was
wrong, and the note carries the reviewer's status; reject: wrong), the reviewer as the
annotator, and
origin customer, so no judge from another model family ever grades it. A regather is no
verdict on the draft and is skipped, as are other approval kinds.

The per-criterion labels are left empty for a person to add: calibration refuses a case
without them. The file holds customer data, so a path inside the repository is refused.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from ccp.approvals import store as approvals_store
from ccp.config import REPO_ROOT
from ccp.storage.scoped import ScopedDB

LABELLED_ACTIONS = {"approve": True, "override": False, "reject": False}
EVENT_ACTIONS = {"approved": "approve", "rejected": "reject"}  # an event without a decision
VERDICT_KIND = "tier3_interrupt"


class InsideRepository(ValueError):
    """Reviewer labels hold customer data; they are never written into the repository."""


def intake(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Labelled-set cases from review-label rows, and a count of what was skipped and why."""
    cases: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    for row in rows:
        if row["kind"] != VERDICT_KIND:
            skipped[f"kind {row['kind']}"] += 1
            continue
        decision = row.get("decision") or {}
        action = decision.get("action") or EVENT_ACTIONS.get(str(decision.get("event")))
        if action not in LABELLED_ACTIONS:
            skipped[f"action {action}"] += 1
            continue
        overall = LABELLED_ACTIONS[str(action)]
        proposed = row.get("proposed") or {}
        draft = proposed.get("draft")
        note = (
            f"review label {row['label_id']} ({row['consent_scope']} consent);"
            " add c1..c5 before calibrating"
        )
        if not overall:
            note += f"; the reviewer's status: {decision.get('status')}"
        cases.append(
            {
                "id": f"rev-{str(row['label_id']).lower()}",
                "kind": "verdict",
                "category": "reviewer_approved" if overall else "reviewer_overridden",
                "rubric": "verdict_reasoning",
                "input": {"ref_id": row["ref_id"], "approval_id": row["approval_id"]},
                "output": dict(draft) if isinstance(draft, dict) else dict(proposed),
                "labels": {},
                "overall": overall,
                "annotator": row["reviewer"],
                "note": note,
                "origin": "customer",
            }
        )
    return cases, dict(skipped)


def outside_repository(out: Path, repo_root: Path = REPO_ROOT) -> Path:
    resolved = out.resolve()
    root = repo_root.resolve()
    if resolved == root or root in resolved.parents:
        raise InsideRepository(
            f"{out} is inside the repository; reviewer labels hold customer data, write them"
            " elsewhere"
        )
    return resolved


def export(db: ScopedDB, out: Path, *, repo_root: Path = REPO_ROOT) -> tuple[int, dict[str, int]]:
    """Write the tenant's consented reviewer labels to ``out``; the count and the skips."""
    target = outside_repository(out, repo_root)
    cases, skipped = intake(approvals_store.labels(db, usable_for="eval"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(cases, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    return len(cases), skipped
