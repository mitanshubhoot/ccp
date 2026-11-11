"""Approval items and their events (spec.md Layer 16). Append-only: the state of an item is
its latest event, read through the approval_status view. A deciding event carries the exact
decision as given (A-95), so whatever acts on it later reads the decision from here.

A decision (approve, reject, ask for changes) is made only by a signed-in member of the
tenant holding the role it needs, never by the item's requester, and never after the item
expires; and an item takes one approve or reject, whatever races, because a unique index
holds the second one back (A-130). An agent or the system may open and close items; only a
person decides them. Approving a remediation or a rollback also mints its one execution
grant, in the same transaction (A-131). An admin may break the glass on an urgent item they
asked for, with a reason, a few times a day, and another approver reviews each use (A-132)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import psycopg
from psycopg import errors as pg_errors
from ulid import ULID

from ccp.approvals import grants
from ccp.auth import members
from ccp.auth.tokens import Principal
from ccp.storage.scoped import ScopedDB

Kind = Literal[
    "tier3_interrupt",
    "remediation",
    "remediation_rollback",
    "policy_draft",
    "evidence_request",
    "exception",
    "identity_merge",
    "break_glass_review",
    "scope_change",
]
Event = Literal["opened", "approved", "rejected", "changes_requested", "closed"]
KINDS: tuple[Kind, ...] = (
    "tier3_interrupt",
    "remediation",
    "remediation_rollback",
    "policy_draft",
    "evidence_request",
    "exception",
    "identity_merge",
    "break_glass_review",
    "scope_change",
)
OPEN_STATES = ("opened", "changes_requested")
DECIDED_STATES = ("approved", "rejected")
# How long an item waits for a decision (A-130); after that nothing acts on it.
EXPIRES: dict[str, timedelta] = {
    "tier3_interrupt": timedelta(days=14),
    "remediation": timedelta(days=3),
    "remediation_rollback": timedelta(days=3),
    "policy_draft": timedelta(days=30),
    "evidence_request": timedelta(days=30),
    "exception": timedelta(days=14),
    "identity_merge": timedelta(days=30),
    "break_glass_review": timedelta(days=7),  # a break-glass use is reviewed within a week
    "scope_change": timedelta(days=14),
}
# Break-glass (A-132): an admin may approve an urgent item they asked for, with a reason,
# a few times a day per tenant; each use opens a review that another approver decides.
BREAK_GLASS_KINDS = frozenset({"remediation", "remediation_rollback"})
BREAK_GLASS_PER_DAY = 2
# The role each decision needs (A-130).
DECISIVE: dict[str, members.Role] = {
    "approved": "approver",
    "rejected": "approver",
    "changes_requested": "reviewer",
}


class Expired(ValueError):
    """The item's time to decide has passed; it must be opened again."""


class AlreadyDecided(ValueError):
    """Someone else decided the item first."""


def payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Approval:
    approval_id: str
    tenant_id: str
    kind: str
    ref_id: str
    payload: dict[str, Any]
    payload_sha256: str
    opened_at: datetime
    state: str
    reviewer: str
    note: str | None
    decided_at: datetime
    decision: dict[str, Any] | None = None
    requested_by: str | None = None
    expires_at: datetime | None = None
    principal_id: str | None = None  # the signed-in member behind the latest event
    break_glass: bool = False  # the latest event was a break-glass approval (A-132)

    def expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and (now or datetime.now(UTC)) >= self.expires_at

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def is_decided(self) -> bool:
        return self.state in DECIDED_STATES


_COLUMNS = (
    "approval_id, tenant_id, kind, ref_id, payload, payload_sha256, opened_at, state, reviewer,"
    " note, decided_at, decision, requested_by, expires_at, principal_id, break_glass"
)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _row(r: tuple[Any, ...]) -> Approval:
    payload = _json(r[4])
    decision = _json(r[11])
    return Approval(
        approval_id=str(r[0]),
        tenant_id=str(r[1]),
        kind=str(r[2]),
        ref_id=str(r[3]),
        payload=dict(payload),
        payload_sha256=str(r[5]),
        opened_at=r[6],
        state=str(r[7]),
        reviewer=str(r[8]),
        note=r[9],
        decided_at=r[10],
        decision=dict(decision) if decision is not None else None,
        requested_by=str(r[12]) if r[12] is not None else None,
        expires_at=r[13],
        principal_id=str(r[14]) if r[14] is not None else None,
        break_glass=bool(r[15]),
    )


def open_item(
    db: ScopedDB,
    kind: Kind,
    ref_id: str,
    payload: dict[str, Any],
    opened_by: str = "system",
    *,
    requested_by: str | None = None,
    expires_in: timedelta | None = None,
) -> str:
    """Open an item and record its `opened` event. Idempotent per (kind, ref_id) while open
    and unexpired. `requested_by` (default: who opened it) may never decide it, and it
    expires after `expires_in` (default: its kind's) (A-130)."""
    existing = for_ref(db, kind, ref_id)
    if existing is not None and existing.is_open and not existing.expired():
        return existing.approval_id
    with db.transaction() as cur:
        return open_item_in(
            cur,
            db.tenant.tenant_id,
            kind,
            ref_id,
            payload,
            opened_by,
            requested_by=requested_by,
            expires_in=expires_in,
        )


def open_item_in(
    cur: psycopg.Cursor[Any],
    tenant_id: str,
    kind: Kind,
    ref_id: str,
    payload: dict[str, Any],
    opened_by: str = "system",
    *,
    requested_by: str | None = None,
    expires_in: timedelta | None = None,
) -> str:
    """Open a new item inside the caller's transaction, so it exists exactly when what it
    asks about does (an identity review, a break-glass use)."""
    approval_id = str(ULID())
    expires_at = datetime.now(UTC) + (expires_in or EXPIRES.get(kind, timedelta(days=7)))
    cur.execute(
        "INSERT INTO approvals (approval_id, tenant_id, kind, ref_id, payload, payload_sha256,"
        " requested_by, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            approval_id,
            tenant_id,
            kind,
            ref_id,
            json.dumps(payload, default=str),
            payload_hash(payload),
            requested_by or opened_by,
            expires_at,
        ),
    )
    cur.execute(
        "INSERT INTO approval_events (event_id, approval_id, tenant_id, event, reviewer)"
        " VALUES (%s, %s, %s, 'opened', %s)",
        (str(ULID()), approval_id, tenant_id, opened_by),
    )
    return approval_id


def _break_glass(db: ScopedDB, current: Approval, event: Event, by: Principal | str) -> str:
    """The checks a break-glass approval passes in place of separation of duties (A-132)."""
    if event != "approved":
        raise ValueError("break-glass approves; it records nothing else")
    if current.kind not in BREAK_GLASS_KINDS:
        raise members.NotPermitted(f"a {current.kind} item is not urgent enough for break-glass")
    if not isinstance(by, Principal):
        raise members.NotPermitted("a decision is made by a signed-in member, never a typed name")
    role = members.require(db, by, "admin")
    if current.expired():
        raise Expired(
            f"approval {current.approval_id!r} expired at {current.expires_at}; open it again"
        )
    used = db.fetch_one(
        "SELECT count(*) FROM approval_events WHERE break_glass"
        " AND recorded_at > now() - interval '1 day'"
    )
    if used is not None and int(used[0]) >= BREAK_GLASS_PER_DAY:
        raise members.NotPermitted(
            f"break-glass was used {used[0]} times in the last day here; that is the limit"
        )
    return role


def _authorize(
    db: ScopedDB, current: Approval, event: Event, by: Principal | str, break_glass: bool = False
) -> tuple[str | None, str | None]:
    """(principal id, role) behind an event; a decision needs a signed-in member with the
    role, who did not ask for it, on an item that has not expired (A-130), unless an admin
    breaks the glass (A-132)."""
    if break_glass:
        role = _break_glass(db, current, event, by)
        assert isinstance(by, Principal)
        return by.principal_id, role
    needed = DECISIVE.get(event)
    if needed is None and event == "closed" and isinstance(by, Principal):
        needed = "reviewer"  # a person closes as a reviewer; the system closes as itself
    if needed is None:
        return None, None
    if not isinstance(by, Principal):
        raise members.NotPermitted("a decision is made by a signed-in member, never a typed name")
    role = members.require(db, by, needed)
    if event in DECISIVE:
        if current.requested_by == by.principal_id:
            raise members.NotPermitted(f"{by.label} asked for this, so may not decide it")
        if current.expired():
            raise Expired(
                f"approval {current.approval_id!r} expired at {current.expires_at}; open it again"
            )
    return by.principal_id, role


def record_event(
    db: ScopedDB,
    approval_id: str,
    event: Event,
    by: Principal | str,
    note: str | None = None,
    *,
    decision: dict[str, Any] | None = None,
    within: Callable[[psycopg.Cursor[Any]], None] | None = None,
    break_glass: str | None = None,
) -> Approval:
    """Append one event, by a signed-in member or, for opening and closing, by the system.
    `within` runs in the same transaction, so whatever it writes (the apply job of a
    decision, A-110) commits with the event or not at all."""
    reviewer = by.label if isinstance(by, Principal) else by
    if not reviewer or not reviewer.strip():
        raise ValueError("a reviewer name is required")
    current = get(db, approval_id)
    if current is None:
        raise LookupError(f"no approval item {approval_id!r}")
    if event in DECIDED_STATES and not current.is_open:
        raise ValueError(f"approval {approval_id!r} is already {current.state}")
    principal_id, role = _authorize(db, current, event, by, break_glass is not None)
    try:
        _append_event(
            db,
            current,
            event,
            reviewer.strip(),
            note,
            decision,
            principal_id,
            role,
            within,
            break_glass,
        )
    except pg_errors.UniqueViolation as exc:
        raise AlreadyDecided(f"approval {approval_id!r} was decided by someone else first") from exc
    updated = get(db, approval_id)
    assert updated is not None
    return updated


def _append_event(
    db: ScopedDB,
    current: Approval,
    event: Event,
    reviewer: str,
    note: str | None,
    decision: dict[str, Any] | None,
    principal_id: str | None,
    role: str | None,
    within: Callable[[psycopg.Cursor[Any]], None] | None,
    break_glass: str | None = None,
) -> None:
    approval_id = current.approval_id
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO approval_events (event_id, approval_id, tenant_id, event, reviewer,"
            " note, decision, principal_id, role, break_glass)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                str(ULID()),
                approval_id,
                db.tenant.tenant_id,
                event,
                reviewer,
                note,
                json.dumps(decision, default=str) if decision is not None else None,
                principal_id,
                role,
                break_glass is not None,
            ),
        )
        if break_glass is not None:  # another approver reviews the use, never this one
            open_item_in(
                cur,
                db.tenant.tenant_id,
                "break_glass_review",
                approval_id,
                {
                    "approval_id": approval_id,
                    "kind": current.kind,
                    "ref_id": current.ref_id,
                    "payload_sha256": current.payload_sha256,
                    "reason": break_glass,
                    "by": principal_id,
                },
                reviewer,
                requested_by=principal_id,
            )
        if event in DECIDED_STATES:  # every decision is also a label (A-100)
            cur.execute(
                "INSERT INTO review_labels (label_id, tenant_id, approval_id, kind, ref_id,"
                " proposed, decision, reviewer, consent_scope)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(ULID()),
                    db.tenant.tenant_id,
                    approval_id,
                    current.kind,
                    current.ref_id,
                    json.dumps(current.payload, default=str),
                    json.dumps(
                        decision if decision is not None else {"event": event, "note": note},
                        default=str,
                    ),
                    reviewer,
                    db.tenant.settings.label_consent,
                ),
            )
        if event == "approved" and current.kind in grants.GRANTED_KINDS:
            grants.mint(
                cur,
                db.tenant.tenant_id,
                approval_id=approval_id,
                proposal_id=current.ref_id,
                payload_sha256=current.payload_sha256,
                precondition_sha256=current.payload.get("precondition_sha256"),
            )
        if within is not None:
            within(cur)


def labels(db: ScopedDB, usable_for: str | None = None) -> list[dict[str, Any]]:
    """Review labels, oldest first; ``usable_for`` "eval" keeps eval and training consent,
    "training" keeps training consent only."""
    scopes = {None: None, "eval": ("eval", "training"), "training": ("training",)}[usable_for]
    clause = " WHERE consent_scope = ANY(%s)" if scopes else ""
    rows = db.fetch_all(
        "SELECT label_id, approval_id, kind, ref_id, proposed, decision, reviewer,"
        f" consent_scope, recorded_at FROM review_labels{clause} ORDER BY recorded_at, label_id",
        (list(scopes),) if scopes else None,
    )
    return [
        {
            "label_id": str(r[0]),
            "approval_id": str(r[1]),
            "kind": str(r[2]),
            "ref_id": str(r[3]),
            "proposed": _json(r[4]),
            "decision": _json(r[5]),
            "reviewer": str(r[6]),
            "consent_scope": str(r[7]),
            "recorded_at": r[8],
        }
        for r in rows
    ]


def get(db: ScopedDB, approval_id: str) -> Approval | None:
    row = db.fetch_one(
        f"SELECT {_COLUMNS} FROM approval_status WHERE approval_id = %s", (approval_id,)
    )
    return _row(row) if row else None


def for_ref(db: ScopedDB, kind: str, ref_id: str) -> Approval | None:
    row = db.fetch_one(
        f"SELECT {_COLUMNS} FROM approval_status WHERE kind = %s AND ref_id = %s"
        " ORDER BY opened_at DESC, approval_id DESC LIMIT 1",
        (kind, ref_id),
    )
    return _row(row) if row else None


def items_for_ref(db: ScopedDB, kind: str, ref_id: str) -> list[Approval]:
    """Every item of one kind for one ref, oldest first."""
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM approval_status WHERE kind = %s AND ref_id = %s"
        " ORDER BY opened_at, approval_id",
        (kind, ref_id),
    )
    return [_row(r) for r in rows]


def approved_for(db: ScopedDB, kind: str, ref_id: str, payload_sha256: str) -> Approval | None:
    """The newest approved item for exactly this payload: an approval binds to what it
    showed, so a request for a different payload neither uses nor revokes it."""
    for item in reversed(items_for_ref(db, kind, ref_id)):
        if item.state == "approved" and item.payload_sha256 == payload_sha256:
            return item
    return None


def list_items(db: ScopedDB, kind: str | None = None, open_only: bool = False) -> list[Approval]:
    clauses: list[str] = []
    params: list[Any] = []
    if kind:
        clauses.append("kind = %s")
        params.append(kind)
    if open_only:
        clauses.append("state IN ('opened', 'changes_requested')")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.fetch_all(f"SELECT {_COLUMNS} FROM approval_status{where} ORDER BY opened_at", params)
    return [_row(r) for r in rows]


def history(db: ScopedDB, approval_id: str) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        "SELECT event, reviewer, note, recorded_at, decision FROM approval_events"
        " WHERE approval_id = %s ORDER BY recorded_at, event_id",
        (approval_id,),
    )
    return [
        {
            "event": str(r[0]),
            "reviewer": str(r[1]),
            "note": r[2],
            "recorded_at": r[3].isoformat(),
            "decision": _json(r[4]),
        }
        for r in rows
    ]


def break_glass_problems(db: ScopedDB, now: datetime | None = None) -> list[str]:
    """Break-glass uses a second approver judged unjustified, or left unreviewed past the
    review's expiry (A-132)."""
    moment = now or datetime.now(UTC)
    problems: list[str] = []
    for review in list_items(db, "break_glass_review"):
        if review.state == "rejected":
            problems.append(
                f"break-glass approval {review.ref_id} was judged unjustified by {review.reviewer}"
            )
        elif review.is_open and review.expired(moment):
            problems.append(f"break-glass approval {review.ref_id} was not reviewed in time")
    return problems
