"""What approving or rejecting an item does (spec.md Layer 16 table).

tier3_interrupt   approve -> resume the graph with approve (or override/regather when asked)
                  reject  -> resume with override to needs_review and the note
remediation       approve -> gate 5 becomes true; the operator still runs remediate execute
                  reject  -> the proposal is marked rejected; re-evaluation does not run
policy_draft      approve -> the draft is marked approved; nothing else happens
evidence_request  closed by the human after uploading; the upload triggers re-evaluation
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ccp.agent import tier3
from ccp.approvals import store
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.storage.scoped import ScopedDB


@dataclass(frozen=True)
class Outcome:
    approval: store.Approval
    detail: str


def approve(
    db: ScopedDB,
    settings: Settings,
    approval_id: str,
    reviewer: str,
    note: str | None = None,
    *,
    decision: str = "approve",
    status: str | None = None,
) -> Outcome:
    item = store.get(db, approval_id)
    if item is None:
        raise LookupError(f"no approval item {approval_id!r}")
    if not item.is_open:
        raise ValueError(f"approval {approval_id!r} is already {item.state}")
    if item.kind == "tier3_interrupt":
        payload: dict[str, Any] = {
            "action": decision,
            "reviewer": reviewer,
            "note": note,
            "status": status,
        }
        verdict = tier3.resume(db, settings, item.ref_id, payload)
        inserted = evaluations.commit_verdict(db, verdict)
        updated = store.record_event(db, approval_id, "approved", reviewer, note)
        return Outcome(updated, f"thread resumed: {verdict.status.value} (inserted={inserted})")
    if item.kind == "remediation":
        updated = store.record_event(db, approval_id, "approved", reviewer, note)
        return Outcome(updated, "gate 5 satisfied; run `ccp remediate execute` with gates 1 to 4")
    updated = store.record_event(db, approval_id, "approved", reviewer, note)
    return Outcome(updated, f"{item.kind} approved")


def reject(db: ScopedDB, settings: Settings, approval_id: str, reviewer: str, note: str) -> Outcome:
    item = store.get(db, approval_id)
    if item is None:
        raise LookupError(f"no approval item {approval_id!r}")
    if not item.is_open:
        raise ValueError(f"approval {approval_id!r} is already {item.state}")
    if item.kind == "tier3_interrupt":
        payload = {
            "action": "override",
            "status": "needs_review",
            "reviewer": reviewer,
            "note": note,
        }
        verdict = tier3.resume(db, settings, item.ref_id, payload)
        evaluations.commit_verdict(db, verdict)
        updated = store.record_event(db, approval_id, "rejected", reviewer, note)
        return Outcome(updated, f"thread resumed as needs_review: {verdict.verdict_id}")
    if item.kind == "remediation":
        db.execute(
            "UPDATE remediation_proposals SET status = 'rejected', updated_at = now()"
            " WHERE proposal_id = %s",
            (item.ref_id,),
        )
    updated = store.record_event(db, approval_id, "rejected", reviewer, note)
    return Outcome(updated, f"{item.kind} rejected")


def request_changes(db: ScopedDB, approval_id: str, reviewer: str, note: str) -> Outcome:
    return Outcome(
        store.record_event(db, approval_id, "changes_requested", reviewer, note),
        "changes requested",
    )


def close(db: ScopedDB, approval_id: str, reviewer: str, note: str | None = None) -> Outcome:
    return Outcome(store.record_event(db, approval_id, "closed", reviewer, note), "closed")
