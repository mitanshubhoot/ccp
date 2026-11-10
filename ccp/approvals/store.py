"""Approval items and their events (spec.md Layer 16). Append-only: the state of an item is
its latest event, read through the approval_status view. A deciding event carries the exact
decision as given (A-95), so whatever acts on it later reads the decision from here."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ulid import ULID

from ccp.storage.scoped import ScopedDB

Kind = Literal[
    "tier3_interrupt", "remediation", "remediation_rollback", "policy_draft", "evidence_request"
]
Event = Literal["opened", "approved", "rejected", "changes_requested", "closed"]
KINDS: tuple[Kind, ...] = (
    "tier3_interrupt",
    "remediation",
    "remediation_rollback",
    "policy_draft",
    "evidence_request",
)
OPEN_STATES = ("opened", "changes_requested")
DECIDED_STATES = ("approved", "rejected")


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

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def is_decided(self) -> bool:
        return self.state in DECIDED_STATES


_COLUMNS = (
    "approval_id, tenant_id, kind, ref_id, payload, payload_sha256, opened_at, state, reviewer,"
    " note, decided_at, decision"
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
    )


def open_item(
    db: ScopedDB, kind: Kind, ref_id: str, payload: dict[str, Any], opened_by: str = "system"
) -> str:
    """Open an item and record its `opened` event. Idempotent per (kind, ref_id) while open."""
    existing = for_ref(db, kind, ref_id)
    if existing is not None and existing.is_open:
        return existing.approval_id
    approval_id = str(ULID())
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO approvals (approval_id, tenant_id, kind, ref_id, payload, payload_sha256)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                approval_id,
                db.tenant.tenant_id,
                kind,
                ref_id,
                json.dumps(payload, default=str),
                payload_hash(payload),
            ),
        )
        cur.execute(
            "INSERT INTO approval_events (event_id, approval_id, tenant_id, event, reviewer)"
            " VALUES (%s, %s, %s, 'opened', %s)",
            (str(ULID()), approval_id, db.tenant.tenant_id, opened_by),
        )
    return approval_id


def record_event(
    db: ScopedDB,
    approval_id: str,
    event: Event,
    reviewer: str,
    note: str | None = None,
    *,
    decision: dict[str, Any] | None = None,
) -> Approval:
    if not reviewer or not reviewer.strip():
        raise ValueError("a reviewer name is required")
    current = get(db, approval_id)
    if current is None:
        raise LookupError(f"no approval item {approval_id!r}")
    if event in DECIDED_STATES and not current.is_open:
        raise ValueError(f"approval {approval_id!r} is already {current.state}")
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO approval_events"
            " (event_id, approval_id, tenant_id, event, reviewer, note, decision)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                str(ULID()),
                approval_id,
                db.tenant.tenant_id,
                event,
                reviewer.strip(),
                note,
                json.dumps(decision, default=str) if decision is not None else None,
            ),
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
                    reviewer.strip(),
                    db.tenant.settings.label_consent,
                ),
            )
    updated = get(db, approval_id)
    assert updated is not None
    return updated


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
