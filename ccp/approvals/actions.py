"""What approving or rejecting an item does (spec.md Layer 16 table), failing closed (A-95).

tier3_interrupt       approve -> record the decision (approve, override with a status, or
                                 regather), then resume the graph from the recorded decision
                      reject  -> record an override to needs_review with the note, then resume
remediation           approve -> gate 5 becomes true and one execution grant is minted (A-131);
                                 the operator still runs remediate execute
                      reject  -> the proposal is marked rejected; re-evaluation does not run
remediation_rollback  approve -> the rollback proposal's gate 5 becomes true and its grant is
                                 minted; remediate rollback still needs gates 1 to 4 live
policy_draft          approve -> the draft is marked approved; nothing else happens
evidence_request      closed by the human after uploading; the upload triggers re-evaluation

Only tier 3 items take a decision other than a plain approve; every other kind refuses one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ulid import ULID

from ccp.agent import decisions
from ccp.approvals import store
from ccp.auth.tokens import Principal
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


def _check_seen(item: store.Approval, seen_sha256: str | None) -> None:
    """A decision is on what the person was shown: a payload hash they were not shown is
    refused before anything is recorded (A-130)."""
    if seen_sha256 is not None and seen_sha256 != item.payload_sha256:
        raise PayloadChanged(
            f"approval {item.approval_id!r} holds payload {item.payload_sha256[:12]}, not the"
            f" {seen_sha256[:12]} that was shown; nothing was recorded"
        )


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
    who: Principal,
    note: str | None = None,
    *,
    decision: str = "approve",
    status: str | None = None,
    providers: Providers | None = None,
    seen_sha256: str | None = None,
) -> Outcome:
    item = _open(db, approval_id)
    _check_seen(item, seen_sha256)
    if item.kind == "tier3_interrupt":
        chosen = decisions.build(decision, who.label, note, status)
        result = decisions.decide(
            db,
            settings,
            approval_id,
            event="approved",
            decision=chosen,
            providers=providers,
            principal=who,
        )
        return _tier3_outcome(result, rejected=False)
    check_plain_decision(item.kind, decision, status)
    updated = store.record_event(db, approval_id, "approved", who, note)
    if item.kind == "remediation":
        return Outcome(
            updated,
            "gate 5 satisfied and one execution grant minted (single use, valid a day); run"
            " `ccp remediate execute` with gates 1 to 4",
        )
    if item.kind == "remediation_rollback":
        return Outcome(
            updated,
            "rollback approved and its grant minted; run `ccp remediate rollback` (gates 1 to 4"
            " when live)",
        )
    return Outcome(updated, f"{item.kind} approved")


def reject(
    db: ScopedDB,
    settings: Settings,
    approval_id: str,
    who: Principal,
    note: str,
    *,
    providers: Providers | None = None,
    seen_sha256: str | None = None,
) -> Outcome:
    item = _open(db, approval_id)
    _check_seen(item, seen_sha256)
    if item.kind == "tier3_interrupt":
        chosen = decisions.build("override", who.label, note, "needs_review")
        result = decisions.decide(
            db,
            settings,
            approval_id,
            event="rejected",
            decision=chosen,
            providers=providers,
            principal=who,
        )
        return _tier3_outcome(result, rejected=True)

    def mark_rejected(cur: Any) -> None:  # with the decision, never before it
        if item.kind in ("remediation", "remediation_rollback"):  # a new event (A-131)
            cur.execute(
                "INSERT INTO proposal_events (event_id, tenant_id, proposal_id, status,"
                " pre_state, planned_call, executed_call)"
                " SELECT %s, tenant_id, proposal_id, 'rejected', pre_state, planned_call,"
                " executed_call FROM proposal_status WHERE proposal_id = %s",
                (str(ULID()), item.ref_id),
            )

    updated = store.record_event(db, approval_id, "rejected", who, note, within=mark_rejected)
    return Outcome(updated, f"{item.kind} rejected")


def resume(
    db: ScopedDB, settings: Settings, approval_id: str, *, providers: Providers | None = None
) -> Outcome:
    """Apply a recorded tier 3 decision whose resume failed."""
    result = decisions.retry(db, settings, approval_id, providers)
    return _tier3_outcome(result, rejected=result.approval.state == "rejected")


def request_changes(db: ScopedDB, approval_id: str, who: Principal, note: str) -> Outcome:
    return Outcome(
        store.record_event(db, approval_id, "changes_requested", who, note),
        "changes requested",
    )


def close(db: ScopedDB, approval_id: str, who: Principal | str, note: str | None = None) -> Outcome:
    return Outcome(store.record_event(db, approval_id, "closed", who, note), "closed")
