"""Reviewer decisions on tier 3 interrupts, fail closed (spec.md Layer 16, A-95).

    build(action, reviewer, note, status)  validate; anything unknown is refused here
    decide(db, settings, approval_id, ...)  item open -> payload unchanged -> RECORD the
                                            decision and QUEUE its apply job, in one
                                            transaction -> apply it now as the job's first
                                            worker
    ApplyJobs (`applies` topic)             apply a recorded decision once: skip it when it was
                                            applied; retry with backoff after a failure
    retry(db, settings, approval_id)        apply a recorded decision by hand
    unapplied(db) / audit(db)               decided but not applied / applied but not decided

The decision is recorded on the approval item before the graph resumes, and the resume reads
it back from there: a decision that is not recorded cannot be applied, and one that is
recorded survives a failed resume. A changed interrupt payload refuses the decision before
anything is recorded, because the reviewer saw something else.

Approvals as data (A-110): the decision and the job that applies it commit together, keyed
by the approval and its payload hash. The process that decided claims the job and applies
it at once; when that fails (a model outage, a crash), the job is retried with backoff by
any worker running `applies`, instead of waiting for someone to notice. Applying is
idempotent: a job whose decision is already applied (the thread finished with its verdict
stored, or a regather led to a newer item) is done without touching anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import psycopg

from ccp.agent import threads, tier3
from ccp.approvals import store as approvals
from ccp.approvals.store import Approval
from ccp.auth.tokens import Principal
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.tier2 import Providers
from ccp.evaluators.verdict import Verdict
from ccp.ingest import event_log
from ccp.ingest.consumers import ParallelConsumer, TerminalError
from ccp.ingest.event_log import LogRecord
from ccp.ingest.worker import register
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus

ACTIONS = ("approve", "override", "regather")
KIND: approvals.Kind = "tier3_interrupt"
APPLY_TOPIC = "applies"


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


def _resume_and_commit(
    db: ScopedDB,
    settings: Settings,
    item: Approval,
    providers: Providers | None,
    now: datetime | None,
    *,
    retry: bool = False,
) -> DecisionOutcome:
    verdict = tier3.resume(
        db, settings, item.ref_id, providers, now, approval_id=item.approval_id, retry=retry
    )
    inserted = evaluations.commit_verdict(db, verdict)
    return DecisionOutcome(item, verdict, inserted, None)


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
        return _resume_and_commit(db, settings, item, providers, now, retry=retry)
    except Exception as exc:
        return DecisionOutcome(item, None, False, f"{type(exc).__name__}: {exc}")


def queue_apply(cur: psycopg.Cursor[Any], db: ScopedDB, item: Approval) -> int:
    """The job that applies this item's decision, written in the decision's transaction."""
    return event_log.append(
        cur,
        tenant_id=db.tenant.tenant_id,
        topic=APPLY_TOPIC,
        partition_key=f"{db.tenant.tenant_id}:approval:{item.approval_id}",
        source_ts=datetime.now(UTC),
        ref=json.dumps({"approval_id": item.approval_id, "payload_sha256": item.payload_sha256}),
    )


class ApplyJobs(ParallelConsumer):
    """Applies recorded tier 3 decisions, once each (A-110)."""

    name = "applies"
    topic = APPLY_TOPIC
    one_per_key = True  # one application of an item at a time

    def __init__(
        self,
        db: ScopedDB,
        settings: Settings,
        providers: Providers | None = None,
        now: datetime | None = None,
    ) -> None:
        super().__init__(
            db,
            max_attempts=settings.ccp_max_attempts,
            visibility_seconds=settings.ccp_claim_visibility_seconds,
            base_delay=settings.ccp_retry_base_seconds,
        )
        self.settings = settings
        self.providers = providers
        self.now = now
        self.outcomes: dict[str, DecisionOutcome] = {}
        self.already_applied = 0

    def handle(self, record: LogRecord) -> None:
        job = json.loads(record.ref)
        item = approvals.get(self._db, str(job["approval_id"]))
        if item is None or item.kind != KIND or not item.is_decided or item.decision is None:
            raise TerminalError(f"approval {job['approval_id']!r} holds no recorded decision")
        if item.payload_sha256 != job["payload_sha256"]:
            raise TerminalError(f"approval {item.approval_id!r} shows another payload")
        if is_applied(self._db, item):
            self.already_applied += 1
            return
        try:
            outcome = _resume_and_commit(
                self._db, self.settings, item, self.providers, self.now, retry=True
            )
        except (LookupError, ValueError, PayloadChanged) as exc:  # retrying cannot help
            self.outcomes[item.approval_id] = DecisionOutcome(item, None, False, str(exc))
            raise TerminalError(f"{type(exc).__name__}: {exc}") from exc
        except Exception as exc:
            self.outcomes[item.approval_id] = DecisionOutcome(
                item, None, False, f"{type(exc).__name__}: {exc}"
            )
            raise
        self.outcomes[item.approval_id] = outcome

    def apply_now(self, offset: int) -> DecisionOutcome | None:
        """Be the first worker of a job this process just queued."""
        with self._db.transaction() as cur:
            record = event_log.claim_offset(cur, offset, self.visibility_seconds, self.owner)
        if record is None:
            return None
        self.work([record])
        job = json.loads(record.ref)
        return self.outcomes.get(str(job["approval_id"]))


register("applies")(lambda db, settings: ApplyJobs(db, settings))


def decide(
    db: ScopedDB,
    settings: Settings,
    approval_id: str,
    *,
    event: Literal["approved", "rejected"],
    decision: dict[str, Any],
    providers: Providers | None = None,
    now: datetime | None = None,
    principal: Principal | None = None,
) -> DecisionOutcome:
    """Record a signed-in member's decision (A-130) and apply it."""
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
    queued: list[int] = []
    recorded = approvals.record_event(
        db,
        approval_id,
        event,
        principal if principal is not None else chosen["reviewer"],
        chosen.get("note"),
        decision=chosen,
        within=lambda cur: queued.append(queue_apply(cur, db, item)),
    )
    outcome = ApplyJobs(db, settings, providers, now).apply_now(queued[0])
    if outcome is None:  # another worker took the job first; it applies the decision
        return DecisionOutcome(recorded, None, False, "the apply job is running elsewhere")
    return DecisionOutcome(recorded, outcome.verdict, outcome.inserted, outcome.error)


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


def is_applied(db: ScopedDB, item: Approval) -> bool:
    """The item's decision took effect: its thread finished with the verdict stored, or a
    regather sent the thread back to the gate, where a newer item now waits."""
    if _applied(db, item.ref_id):
        return True
    if (item.decision or {}).get("action") != "regather":
        return False
    return any(
        later.opened_at > item.opened_at for later in approvals.items_for_ref(db, KIND, item.ref_id)
    )


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
    return [
        item
        for item in approvals.list_items(db, KIND)
        if item.is_decided and not is_applied(db, item)
    ]


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
