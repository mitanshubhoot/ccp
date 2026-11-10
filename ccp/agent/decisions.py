"""Reviewer decisions on tier 3 interrupts, fail closed (spec.md Layer 16, A-95).

    build(action, reviewer, note, status)  validate; anything unknown is refused here
    decide(db, settings, approval_id, ...)  item open -> payload unchanged -> RECORD -> apply
    retry(db, settings, approval_id)        apply a recorded decision whose resume failed
    unapplied(db) / audit(db)               decided but not applied / applied but not decided

The decision is recorded on the approval item before the graph resumes, and the resume reads
it back from there: a decision that is not recorded cannot be applied, and one that is
recorded survives a failed resume. A changed interrupt payload refuses the decision before
anything is recorded, because the reviewer saw something else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from ccp.agent import threads, tier3
from ccp.approvals import store as approvals
from ccp.approvals.store import Approval
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.tier2 import Providers
from ccp.evaluators.verdict import Verdict
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus

ACTIONS = ("approve", "override", "regather")
KIND: approvals.Kind = "tier3_interrupt"


PayloadChanged = tier3.PayloadChanged


class InvalidDecision(ValueError):
    """A decision that names no known action, no reviewer, or no valid override status."""


def build(
    action: str, reviewer: str, note: str | None = None, status: str | None = None
) -> dict[str, Any]:
    """The only constructor of a decision. Unknown actions never default to approve."""
    if action not in ACTIONS:
        raise InvalidDecision(f"decision must be one of {', '.join(ACTIONS)}; got {action!r}")
    if not reviewer or not reviewer.strip():
        raise InvalidDecision("a reviewer name is required")
    if action == "override":
        if not status:
            raise InvalidDecision("an override needs --status")
        try:
            VerdictStatus(status)
        except ValueError:
            allowed = ", ".join(s.value for s in VerdictStatus)
            raise InvalidDecision(f"status must be one of {allowed}; got {status!r}") from None
    elif status is not None:
        raise InvalidDecision(f"--status applies only to override, not {action}")
    return {
        "action": action,
        "reviewer": reviewer.strip(),
        "note": note,
        "status": status,
        "decided_at": datetime.now(UTC).isoformat(),
    }


def validate(decision: dict[str, Any] | None) -> dict[str, Any]:
    """Re-check a decision that arrives from storage or a caller, same rules as `build`."""
    if not isinstance(decision, dict):
        raise InvalidDecision("no decision")
    rebuilt = build(
        str(decision.get("action", "")),
        str(decision.get("reviewer") or ""),
        decision.get("note"),
        decision.get("status"),
    )
    return {**rebuilt, "decided_at": decision.get("decided_at", rebuilt["decided_at"])}


@dataclass(frozen=True)
class DecisionOutcome:
    approval: Approval
    verdict: Verdict | None
    inserted: bool
    error: str | None

    @property
    def resumed(self) -> bool:
        return self.verdict is not None and self.error is None


def _open_tier3_item(db: ScopedDB, approval_id: str) -> Approval:
    item = approvals.get(db, approval_id)
    if item is None:
        raise LookupError(f"no approval item {approval_id!r}")
    if item.kind != KIND:
        raise ValueError(f"approval {approval_id!r} is a {item.kind}, not a {KIND}")
    if not item.is_open:
        raise ValueError(f"approval {approval_id!r} is already {item.state}")
    return item


def _apply(
    db: ScopedDB,
    settings: Settings,
    item: Approval,
    providers: Providers | None,
    now: datetime | None,
    *,
    retry: bool = False,
) -> DecisionOutcome:
    """Resume from the recorded decision and commit the verdict. A failure leaves the decision
    recorded and the thread failed; `retry` picks it up from there."""
    try:
        verdict = tier3.resume(
            db, settings, item.ref_id, providers, now, approval_id=item.approval_id, retry=retry
        )
        inserted = evaluations.commit_verdict(db, verdict)
    except Exception as exc:
        return DecisionOutcome(item, None, False, f"{type(exc).__name__}: {exc}")
    return DecisionOutcome(item, verdict, inserted, None)


def decide(
    db: ScopedDB,
    settings: Settings,
    approval_id: str,
    *,
    event: Literal["approved", "rejected"],
    decision: dict[str, Any],
    providers: Providers | None = None,
    now: datetime | None = None,
) -> DecisionOutcome:
    item = _open_tier3_item(db, approval_id)
    chosen = validate(decision)
    if event == "rejected" and (
        chosen["action"] != "override" or chosen["status"] != VerdictStatus.needs_review.value
    ):
        raise InvalidDecision("a rejection resumes as an override to needs_review")
    pending = tier3.pending_payload_sha256(db, settings, item.ref_id, providers)
    if pending != item.payload_sha256:
        raise PayloadChanged(
            f"thread {item.ref_id!r} is no longer waiting on the payload approval"
            f" {approval_id!r} showed; nothing was recorded"
        )
    recorded = approvals.record_event(
        db, approval_id, event, chosen["reviewer"], chosen.get("note"), decision=chosen
    )
    return _apply(db, settings, recorded, providers, now)


def retry(
    db: ScopedDB,
    settings: Settings,
    approval_id: str,
    providers: Providers | None = None,
    now: datetime | None = None,
) -> DecisionOutcome:
    """Apply a recorded decision again after its resume failed. The resume still checks that
    the item's payload is the pending interrupt, or that the thread went past the gate with
    exactly this decision, so a stale decision cannot be applied to a later interrupt."""
    item = approvals.get(db, approval_id)
    if item is None:
        raise LookupError(f"no approval item {approval_id!r}")
    if item.kind != KIND or not item.is_decided or item.decision is None:
        raise ValueError(f"approval {approval_id!r} holds no recorded tier 3 decision")
    if _applied(db, item.ref_id):
        raise ValueError(f"thread {item.ref_id!r} finished and its verdict is stored")
    return _apply(db, settings, item, providers, now, retry=True)


def _applied(db: ScopedDB, thread_id: str) -> bool:
    """Finished, and the verdict it finished with is stored (a failed commit leaves it not)."""
    row = threads.get(db, thread_id)
    return (
        row is not None
        and row.status == "finished"
        and bool(row.verdict_id)
        and verdicts.load(db, str(row.verdict_id)) is not None
    )


def unapplied(db: ScopedDB) -> list[Approval]:
    """Decided items whose thread has not finished with a stored verdict. A regather that
    led to a newer item for its thread was applied: that newer item is what waits now."""
    items = approvals.list_items(db, KIND)  # ordered by opened_at
    found: list[Approval] = []
    for n, item in enumerate(items):
        if not item.is_decided:
            continue
        superseded = any(later.ref_id == item.ref_id for later in items[n + 1 :])
        if superseded and (item.decision or {}).get("action") == "regather":
            continue
        if not _applied(db, item.ref_id):
            found.append(item)
    return found


def audit(db: ScopedDB) -> list[str]:
    """Finished tier 3 threads that applied a decision no reviewer recorded."""
    recorded: dict[str, list[dict[str, Any]]] = {}
    for item in approvals.list_items(db, KIND):
        for event in approvals.history(db, item.approval_id):
            if event["event"] in approvals.DECIDED_STATES and event["decision"] is not None:
                recorded.setdefault(item.ref_id, []).append(dict(event["decision"]))
    problems: list[str] = []
    for row in threads.all_threads(db, limit=10_000):
        if row.graph != tier3.GRAPH or row.status != "finished" or row.decision is None:
            continue
        if row.decision not in recorded.get(row.thread_id, []):
            problems.append(f"thread {row.thread_id} applied a decision with no recorded approval")
    return problems
