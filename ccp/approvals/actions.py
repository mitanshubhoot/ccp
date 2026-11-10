"""What approving or rejecting an item does (spec.md Layer 16 table), failing closed (A-95).

tier3_interrupt       approve -> record the decision (approve, override with a status, or
                                 regather), then resume the graph from the recorded decision
                      reject  -> record an override to needs_review with the note, then resume
remediation           approve -> gate 5 becomes true; the operator still runs remediate execute
                      reject  -> the proposal is marked rejected; re-evaluation does not run
remediation_rollback  approve -> the rollback's gate 5 becomes true; remediate rollback still
                                 needs gates 1 to 4 for a live proposal
policy_draft          approve -> the draft is marked approved; nothing else happens
evidence_request      closed by the human after uploading; the upload triggers re-evaluation

Only tier 3 items take a decision other than a plain approve; every other kind refuses one.
"""

from __future__ import annotations

from dataclasses import dataclass

from ccp.agent import decisions
from ccp.approvals import store
from ccp.config import Settings
from ccp.evaluators.tier2 import Providers
from ccp.storage.scoped import ScopedDB

InvalidDecision = decisions.InvalidDecision
PayloadChanged = decisions.PayloadChanged


@dataclass(frozen=True)
class Outcome:
    approval: store.Approval
    detail: str
    ok: bool = True


def _open(db: ScopedDB, approval_id: str) -> store.Approval:
    item = store.get(db, approval_id)
    if item is None:
        raise LookupError(f"no approval item {approval_id!r}")
    if not item.is_open:
        raise ValueError(f"approval {approval_id!r} is already {item.state}")
    return item


def check_plain_decision(kind: str, decision: str, status: str | None) -> None:
    """Items other than tier 3 interrupts are approved as they are; a decision or a status
    meant for a tier 3 item is refused, never read as approve."""
    if decision != "approve" or status is not None:
        raise InvalidDecision(
            f"a {kind} item takes a plain approve; --decision and --status apply to"
            " tier3_interrupt items only"
        )


def _tier3_outcome(result: decisions.DecisionOutcome, rejected: bool) -> Outcome:
    if result.verdict is None or result.error is not None:
        return Outcome(
            result.approval,
            f"decision recorded; resume failed: {result.error}. Its apply job retries it"
            " (`ccp worker run --consumer applies`), or run"
            f" `ccp approvals resume {result.approval.approval_id}` to apply it now",
            ok=False,
        )
    if rejected:
        return Outcome(
            result.approval,
            f"thread resumed as {result.verdict.status.value}: {result.verdict.verdict_id}",
        )
    return Outcome(
        result.approval,
        f"thread resumed: {result.verdict.status.value} (inserted={result.inserted})",
    )


def approve(
    db: ScopedDB,
    settings: Settings,
    approval_id: str,
    reviewer: str,
    note: str | None = None,
    *,
    decision: str = "approve",
    status: str | None = None,
    providers: Providers | None = None,
) -> Outcome:
    item = _open(db, approval_id)
    if item.kind == "tier3_interrupt":
        chosen = decisions.build(decision, reviewer, note, status)
        result = decisions.decide(
            db, settings, approval_id, event="approved", decision=chosen, providers=providers
        )
        return _tier3_outcome(result, rejected=False)
    check_plain_decision(item.kind, decision, status)
    updated = store.record_event(db, approval_id, "approved", reviewer, note)
    if item.kind == "remediation":
        return Outcome(updated, "gate 5 satisfied; run `ccp remediate execute` with gates 1 to 4")
    if item.kind == "remediation_rollback":
        return Outcome(
            updated, "rollback approved; run `ccp remediate rollback` (gates 1 to 4 when live)"
        )
    return Outcome(updated, f"{item.kind} approved")


def reject(
    db: ScopedDB,
    settings: Settings,
    approval_id: str,
    reviewer: str,
    note: str,
    *,
    providers: Providers | None = None,
) -> Outcome:
    item = _open(db, approval_id)
    if item.kind == "tier3_interrupt":
        chosen = decisions.build("override", reviewer, note, "needs_review")
        result = decisions.decide(
            db, settings, approval_id, event="rejected", decision=chosen, providers=providers
        )
        return _tier3_outcome(result, rejected=True)
    if item.kind == "remediation":
        db.execute(
            "UPDATE remediation_proposals SET status = 'rejected', updated_at = now()"
            " WHERE proposal_id = %s",
            (item.ref_id,),
        )
    updated = store.record_event(db, approval_id, "rejected", reviewer, note)
    return Outcome(updated, f"{item.kind} rejected")


def resume(
    db: ScopedDB, settings: Settings, approval_id: str, *, providers: Providers | None = None
) -> Outcome:
    """Apply a recorded tier 3 decision whose resume failed."""
    result = decisions.retry(db, settings, approval_id, providers)
    return _tier3_outcome(result, rejected=result.approval.state == "rejected")


def request_changes(db: ScopedDB, approval_id: str, reviewer: str, note: str) -> Outcome:
    return Outcome(
        store.record_event(db, approval_id, "changes_requested", reviewer, note),
        "changes requested",
    )


def close(db: ScopedDB, approval_id: str, reviewer: str, note: str | None = None) -> Outcome:
    return Outcome(store.record_event(db, approval_id, "closed", reviewer, note), "closed")
