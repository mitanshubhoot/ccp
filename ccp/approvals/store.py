"""Approval items and their events (spec.md Layer 16). Append-only: the state of an item is
its latest event, read through the approval_status view."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ulid import ULID

from ccp.storage.scoped import ScopedDB

Kind = Literal["tier3_interrupt", "remediation", "policy_draft", "evidence_request"]
Event = Literal["opened", "approved", "rejected", "changes_requested", "closed"]
KINDS: tuple[Kind, ...] = ("tier3_interrupt", "remediation", "policy_draft", "evidence_request")
OPEN_STATES = ("opened", "changes_requested")


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

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES


_COLUMNS = (
    "approval_id, tenant_id, kind, ref_id, payload, payload_sha256, opened_at, state, reviewer,"
    " note, decided_at"
)


def _row(r: tuple[Any, ...]) -> Approval:
    payload = json.loads(r[4]) if isinstance(r[4], str) else dict(r[4])
    return Approval(
        approval_id=str(r[0]),
        tenant_id=str(r[1]),
        kind=str(r[2]),
        ref_id=str(r[3]),
        payload=payload,
        payload_sha256=str(r[5]),
        opened_at=r[6],
        state=str(r[7]),
        reviewer=str(r[8]),
        note=r[9],
        decided_at=r[10],
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
    db: ScopedDB, approval_id: str, event: Event, reviewer: str, note: str | None = None
) -> Approval:
    if not reviewer or not reviewer.strip():
        raise ValueError("a reviewer name is required")
    current = get(db, approval_id)
    if current is None:
        raise LookupError(f"no approval item {approval_id!r}")
    if event in ("approved", "rejected") and not current.is_open:
        raise ValueError(f"approval {approval_id!r} is already {current.state}")
    db.execute(
        "INSERT INTO approval_events (event_id, approval_id, tenant_id, event, reviewer, note)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (str(ULID()), approval_id, db.tenant.tenant_id, event, reviewer.strip(), note),
    )
    updated = get(db, approval_id)
    assert updated is not None
    return updated


def get(db: ScopedDB, approval_id: str) -> Approval | None:
    row = db.fetch_one(
        f"SELECT {_COLUMNS} FROM approval_status WHERE approval_id = %s", (approval_id,)
    )
    return _row(row) if row else None


def for_ref(db: ScopedDB, kind: str, ref_id: str) -> Approval | None:
    row = db.fetch_one(
        f"SELECT {_COLUMNS} FROM approval_status WHERE kind = %s AND ref_id = %s"
        " ORDER BY opened_at DESC LIMIT 1",
        (kind, ref_id),
    )
    return _row(row) if row else None


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
        "SELECT event, reviewer, note, recorded_at FROM approval_events WHERE approval_id = %s"
        " ORDER BY recorded_at, event_id",
        (approval_id,),
    )
    return [
        {"event": str(r[0]), "reviewer": str(r[1]), "note": r[2], "recorded_at": r[3].isoformat()}
        for r in rows
    ]
