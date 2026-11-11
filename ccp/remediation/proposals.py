"""Remediation proposals and the approval item each one waits in (spec.md Layer 10, A-131).

A proposal is never edited. Its row holds what was proposed: the action and target, the
risk summary code writes from them, the precondition it was proposed under and that
precondition's hash, and for a rollback the proposal it reverses. Each change of status is
a new proposal_events row carrying the call, pre-state and refusal as of that change, and
proposal_status lays the latest one over the row. The approval payload is the proposal's
payload: what is changed, the risk, and the precondition hash, so approving it approves
exactly that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from ulid import ULID

from ccp.approvals import store as approvals
from ccp.knowledge.actions import REVERSALS, risk_summary
from ccp.remediation import preconditions
from ccp.storage.scoped import ScopedDB

ROLLBACK_KIND: approvals.Kind = "remediation_rollback"
# Statuses that say what happened; once one is recorded, only another of them follows.
FINAL = ("executed", "simulated", "rolled_back", "rejected")

_COLUMNS = (
    "proposal_id, tenant_id, verdict_id, source, action, target, rationale, reversible,"
    " live_capable, payload_sha256, status, pre_state, planned_call, executed_call, refusal,"
    " created_at, updated_at, reverses, risk, precondition, precondition_sha256"
)
_UNSET: Any = object()


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    tenant_id: str
    verdict_id: str
    source: str
    action: str
    target: dict[str, str]
    rationale: str
    reversible: bool
    live_capable: bool
    payload_sha256: str
    status: str
    pre_state: dict[str, Any] | None
    planned_call: dict[str, Any] | None
    executed_call: dict[str, Any] | None
    refusal: str | None
    created_at: datetime
    updated_at: datetime
    reverses: str | None = None
    risk: dict[str, Any] | None = None
    precondition: dict[str, Any] | None = None
    precondition_sha256: str = ""

    @property
    def kind(self) -> approvals.Kind:
        """The approval item kind it waits in."""
        return ROLLBACK_KIND if self.reverses else "remediation"

    def payload(self) -> dict[str, Any]:
        """What the approval shows and its hash binds: the change, its risk, and the
        precondition it is for."""
        return payload_of(
            self.tenant_id,
            self.verdict_id,
            self.action,
            self.target,
            risk=self.risk or {},
            precondition_sha256=self.precondition_sha256,
            reverses=self.reverses,
        )

    def current_hash(self) -> str:
        return approvals.payload_hash(self.payload())


def payload_of(
    tenant_id: str,
    verdict_id: str,
    action: str,
    target: dict[str, str],
    *,
    risk: dict[str, Any],
    precondition_sha256: str,
    reverses: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "tenant_id": tenant_id,
        "verdict_id": verdict_id,
        "action": action,
        "target": target,
        "risk": risk,
        "precondition_sha256": precondition_sha256,
    }
    if reverses is not None:
        body["reverses"] = reverses
    return body


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _row(r: tuple[Any, ...]) -> Proposal:
    return Proposal(
        proposal_id=str(r[0]),
        tenant_id=str(r[1]),
        verdict_id=str(r[2]),
        source=str(r[3]),
        action=str(r[4]),
        target={str(k): str(v) for k, v in dict(_load(r[5])).items()},
        rationale=str(r[6]),
        reversible=bool(r[7]),
        live_capable=bool(r[8]),
        payload_sha256=str(r[9]),
        status=str(r[10]),
        pre_state=_load(r[11]) if r[11] is not None else None,
        planned_call=_load(r[12]) if r[12] is not None else None,
        executed_call=_load(r[13]) if r[13] is not None else None,
        refusal=r[14],
        created_at=r[15],
        updated_at=r[16],
        reverses=str(r[17]) if r[17] is not None else None,
        risk=dict(_load(r[18])) if r[18] is not None else None,
        precondition=dict(_load(r[19])) if r[19] is not None else None,
        precondition_sha256=str(r[20] or ""),
    )


def create(
    db: ScopedDB,
    *,
    verdict_id: str,
    source: str,
    action: str,
    target: dict[str, str],
    rationale: str,
    reversible: bool,
    live_capable: bool,
    reverses: Proposal | None = None,
    requested_by: str | None = None,
) -> Proposal:
    """Insert the proposal, with its risk summary and precondition, and open its approval
    item (kind remediation, or remediation_rollback for a rollback)."""
    proposal_id = str(ULID())
    risk = risk_summary(action, target, reversible=reversible, live_capable=live_capable)
    facts = preconditions.current(
        db, verdict_id=verdict_id, action=action, target=target, reverses=reverses
    )
    facts_sha256 = preconditions.digest(facts)
    reversed_id = reverses.proposal_id if reverses is not None else None
    payload = payload_of(
        db.tenant.tenant_id,
        verdict_id,
        action,
        target,
        risk=risk,
        precondition_sha256=facts_sha256,
        reverses=reversed_id,
    )
    db.execute(
        "INSERT INTO remediation_proposals (proposal_id, tenant_id, verdict_id, source, action,"
        " target, rationale, reversible, live_capable, payload_sha256, reverses, risk,"
        " precondition, precondition_sha256)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            proposal_id,
            db.tenant.tenant_id,
            verdict_id,
            source,
            action,
            json.dumps(target),
            rationale,
            reversible,
            live_capable,
            approvals.payload_hash(payload),
            reversed_id,
            json.dumps(risk),
            json.dumps(facts, default=str),
            facts_sha256,
        ),
    )
    approvals.open_item(
        db,
        ROLLBACK_KIND if reverses is not None else "remediation",
        proposal_id,
        payload,
        requested_by=requested_by,
    )
    created = get(db, proposal_id)
    assert created is not None
    return created


def create_reversal(
    db: ScopedDB, original: Proposal, target: dict[str, str], requested_by: str | None = None
) -> Proposal:
    """A rollback is a proposal of its own: the action that undoes the original, with the
    permission recorded before it acted, waiting for its own approval."""
    action = REVERSALS[original.action]
    return create(
        db,
        verdict_id=original.verdict_id,
        source="rollback",
        action=action,
        target=target,
        rationale=f"undo {original.action} of proposal {original.proposal_id}",
        reversible=False,
        live_capable=original.live_capable and action == "restore_repo_collaborator",
        reverses=original,
        requested_by=requested_by,
    )


def get(db: ScopedDB, proposal_id: str) -> Proposal | None:
    row = db.fetch_one(
        f"SELECT {_COLUMNS} FROM proposal_status WHERE proposal_id = %s", (proposal_id,)
    )
    return _row(row) if row else None


def for_verdict(db: ScopedDB, verdict_id: str) -> list[Proposal]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM proposal_status WHERE verdict_id = %s AND reverses IS NULL"
        " ORDER BY created_at, proposal_id",
        (verdict_id,),
    )
    return [_row(r) for r in rows]


def reversals(db: ScopedDB, proposal_id: str) -> list[Proposal]:
    """The rollback proposals made for one proposal, oldest first."""
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM proposal_status WHERE reverses = %s"
        " ORDER BY created_at, proposal_id",
        (proposal_id,),
    )
    return [_row(r) for r in rows]


def list_all(db: ScopedDB, status: str | None = None) -> list[Proposal]:
    if status:
        rows = db.fetch_all(
            f"SELECT {_COLUMNS} FROM proposal_status WHERE status = %s"
            " ORDER BY created_at, proposal_id",
            (status,),
        )
    else:
        rows = db.fetch_all(
            f"SELECT {_COLUMNS} FROM proposal_status ORDER BY created_at, proposal_id"
        )
    return [_row(r) for r in rows]


def record(
    db: ScopedDB,
    proposal_id: str,
    status: str,
    *,
    pre_state: Any = _UNSET,
    planned_call: Any = _UNSET,
    executed_call: Any = _UNSET,
    refusal: str | None = None,
) -> Proposal:
    """Append one status change in its own transaction and return the proposal after it."""
    with db.transaction() as cur:
        record_in(
            cur,
            db.tenant.tenant_id,
            proposal_id,
            status,
            pre_state=pre_state,
            planned_call=planned_call,
            executed_call=executed_call,
            refusal=refusal,
        )
    updated = get(db, proposal_id)
    assert updated is not None
    return updated


def record_in(
    cur: psycopg.Cursor[Any],
    tenant_id: str,
    proposal_id: str,
    status: str,
    *,
    pre_state: Any = _UNSET,
    planned_call: Any = _UNSET,
    executed_call: Any = _UNSET,
    refusal: str | None = None,
) -> None:
    """Append one status change inside the caller's transaction. Fields not given carry
    over from the latest event; the refusal is the one given, so a later change clears it."""
    _append(cur, tenant_id, proposal_id, status, pre_state, planned_call, executed_call, refusal)


def _append(
    cur: psycopg.Cursor[Any],
    tenant_id: str,
    proposal_id: str,
    status: str,
    pre_state: Any,
    planned_call: Any,
    executed_call: Any,
    refusal: str | None,
) -> None:
    current = cur.execute(
        "SELECT pre_state, planned_call, executed_call, status FROM proposal_status"
        " WHERE proposal_id = %s",
        (proposal_id,),
    ).fetchone()
    if current is None:
        raise LookupError(f"no proposal {proposal_id!r}")
    if current[3] in FINAL and status not in FINAL:
        return  # what happened stands: a late refusal never reads as the proposal's status

    def keep(given: Any, was: Any) -> str | None:
        value = _load(was) if given is _UNSET else given
        return json.dumps(value, default=str) if value is not None else None

    cur.execute(
        "INSERT INTO proposal_events (event_id, tenant_id, proposal_id, status, pre_state,"
        " planned_call, executed_call, refusal) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            str(ULID()),
            tenant_id,
            proposal_id,
            status,
            keep(pre_state, current[0]),
            keep(planned_call, current[1]),
            keep(executed_call, current[2]),
            refusal,
        ),
    )
