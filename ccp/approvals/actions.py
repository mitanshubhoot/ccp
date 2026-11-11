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
exception             approve -> the exception is minted: the subject reads "fail (excepted
                                 until D)" until D, and still counts as a failure (A-132)
identity_merge        approve -> the two records are linked as one person; reject -> they are
                                 kept apart; either way the review closes and the people
                                 involved are evaluated again (A-132)
break_glass_review    approve -> the break-glass use was justified; reject -> it was not, and
                                 the approvals audit reports it (A-132)

An admin may approve an urgent item (a remediation or its rollback) they asked for by
break-glass, with a reason; another approver then reviews the use (A-132).

Only tier 3 items take a decision other than a plain approve; every other kind refuses one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from ccp.agent import decisions
from ccp.approvals import store
from ccp.auth.tokens import Principal
from ccp.config import Settings
from ccp.evaluators.tier2 import Providers
from ccp.identity import resolver
from ccp.storage import exceptions as stored_exceptions
from ccp.storage.scoped import ScopedDB

MIN_BREAK_GLASS_REASON = 20  # characters

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
    if item.kind == "exception":
        return _grant_exception(db, item, who, note)
    if item.kind == "identity_merge":
        write = resolver.resolution_writer(
            db, item.ref_id, "merge", who.label, note, approval_id=approval_id
        )
        updated = store.record_event(db, approval_id, "approved", who, note, within=write)
        return Outcome(updated, "linked as one person; the people involved are evaluated again")
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

    kept_apart = (
        resolver.resolution_writer(
            db, item.ref_id, "distinct", who.label, note, approval_id=approval_id
        )
        if item.kind == "identity_merge"
        else None
    )

    def mark_rejected(cur: Any) -> None:  # with the decision, never before it
        if kept_apart is not None:
            kept_apart(cur)
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


def _grant_exception(
    db: ScopedDB, item: store.Approval, who: Principal, note: str | None
) -> Outcome:
    """Approving an exception mints it in the decision's transaction, from the payload the
    approver saw; one whose date has passed meanwhile is refused (A-132)."""
    asked = item.payload
    until = datetime.fromisoformat(str(asked["until"]))
    if until <= datetime.now(UTC):
        raise store.Expired(f"the exception asked for ended at {until}; ask again")

    def grant(cur: Any) -> None:
        stored_exceptions.insert(
            cur,
            db.tenant.tenant_id,
            approval_id=item.approval_id,
            test_id=str(asked["test_id"]),
            subject_key=str(asked["subject_key"]),
            reason=str(asked["reason"]),
            owner=str(asked["owner"]),
            valid_until=until,
            granted_by=who.principal_id,
        )

    updated = store.record_event(db, item.approval_id, "approved", who, note, within=grant)
    return Outcome(
        updated,
        f"exception granted: {asked['subject_key']} shows as {asked['shows_as']}, never as pass",
    )


def break_glass(
    db: ScopedDB,
    settings: Settings,
    approval_id: str,
    who: Principal,
    reason: str,
    *,
    seen_sha256: str | None = None,
) -> Outcome:
    """Approve an urgent item an admin asked for themselves, with a reason. The approval
    mints its grant like any other; a break_glass_review item opens at once for another
    approver, and the approvals audit reports a use left unreviewed or judged unjustified."""
    if len(reason.strip()) < MIN_BREAK_GLASS_REASON:
        raise ValueError(f"say why in at least {MIN_BREAK_GLASS_REASON} characters")
    item = _open(db, approval_id)
    _check_seen(item, seen_sha256)
    updated = store.record_event(
        db, approval_id, "approved", who, reason.strip(), break_glass=reason.strip()
    )
    review = store.for_ref(db, "break_glass_review", approval_id)
    return Outcome(
        updated,
        f"approved by break-glass; approval {review.approval_id if review else '?'} asks another"
        f" approver to review this use within {store.EXPIRES['break_glass_review'].days} days",
    )


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
