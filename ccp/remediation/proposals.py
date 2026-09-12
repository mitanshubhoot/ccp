"""remediation_proposals rows and the approval item each one waits in."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ulid import ULID

from ccp.approvals import store as approvals
from ccp.storage.scoped import ScopedDB

_COLUMNS = (
    "proposal_id, tenant_id, verdict_id, source, action, target, rationale, reversible,"
    " live_capable, payload_sha256, status, pre_state, planned_call, executed_call, refusal,"
    " created_at, updated_at"
)


def payload_hash(tenant_id: str, verdict_id: str, action: str, target: dict[str, Any]) -> str:
    material = json.dumps(
        {"tenant_id": tenant_id, "verdict_id": verdict_id, "action": action, "target": target},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


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

    def current_hash(self) -> str:
        return payload_hash(self.tenant_id, self.verdict_id, self.action, self.target)


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
) -> Proposal:
    """Insert the proposal and open its approval item (kind remediation)."""
    proposal_id = str(ULID())
    digest = payload_hash(db.tenant.tenant_id, verdict_id, action, target)
    db.execute(
        "INSERT INTO remediation_proposals (proposal_id, tenant_id, verdict_id, source, action,"
        " target, rationale, reversible, live_capable, payload_sha256)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
            digest,
        ),
    )
    approvals.open_item(
        db,
        "remediation",
        proposal_id,
        {
            "tenant_id": db.tenant.tenant_id,
            "verdict_id": verdict_id,
            "action": action,
            "target": target,
        },
    )
    created = get(db, proposal_id)
    assert created is not None
    return created


def get(db: ScopedDB, proposal_id: str) -> Proposal | None:
    row = db.fetch_one(
        f"SELECT {_COLUMNS} FROM remediation_proposals WHERE proposal_id = %s", (proposal_id,)
    )
    return _row(row) if row else None


def for_verdict(db: ScopedDB, verdict_id: str) -> list[Proposal]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM remediation_proposals WHERE verdict_id = %s ORDER BY created_at",
        (verdict_id,),
    )
    return [_row(r) for r in rows]


def list_all(db: ScopedDB, status: str | None = None) -> list[Proposal]:
    if status:
        rows = db.fetch_all(
            f"SELECT {_COLUMNS} FROM remediation_proposals WHERE status = %s ORDER BY created_at",
            (status,),
        )
    else:
        rows = db.fetch_all(f"SELECT {_COLUMNS} FROM remediation_proposals ORDER BY created_at")
    return [_row(r) for r in rows]


def update(db: ScopedDB, proposal_id: str, **fields: Any) -> Proposal:
    allowed = {"status", "pre_state", "planned_call", "executed_call", "refusal"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot update {sorted(unknown)}")
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        sets.append(f"{key} = %s")
        params.append(
            json.dumps(value, default=str)
            if key.endswith(("state", "call")) and value is not None
            else value
        )
    params.append(proposal_id)
    db.execute(
        f"UPDATE remediation_proposals SET {', '.join(sets)}, updated_at = now()"
        " WHERE proposal_id = %s",
        params,
    )
    updated = get(db, proposal_id)
    assert updated is not None
    return updated
