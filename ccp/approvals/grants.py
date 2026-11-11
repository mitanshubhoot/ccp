"""Execution grants (spec.md A-131).

Approving a remediation or a rollback item mints exactly one grant, in the decision's own
transaction: bound to the item, its payload hash and its precondition hash, and valid for a
day. The executor consumes it once, in the transaction that opens its journal entry, and a
key on the use holds back a second one whatever races. Holding a proposal id or an approval
id authorizes nothing: only an unused, unexpired grant for the exact payload does, and a
failed or interrupted attempt spends it, so trying again takes a new decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from ulid import ULID

from ccp.storage.scoped import ScopedDB

GRANTED_KINDS = frozenset({"remediation", "remediation_rollback"})
GRANT_TTL = timedelta(hours=24)


class GrantUsed(PermissionError):
    """The grant was consumed already: a second use needs a new decision."""


@dataclass(frozen=True)
class Grant:
    grant_id: str
    tenant_id: str
    approval_id: str
    proposal_id: str
    payload_sha256: str
    precondition_sha256: str | None
    issued_at: datetime
    expires_at: datetime

    def expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at


@dataclass(frozen=True)
class Use:
    grant_id: str
    operation: str
    used_at: datetime


_COLUMNS = (
    "grant_id, tenant_id, approval_id, proposal_id, payload_sha256, precondition_sha256,"
    " issued_at, expires_at"
)


def _grant(r: tuple[Any, ...]) -> Grant:
    return Grant(
        grant_id=str(r[0]),
        tenant_id=str(r[1]),
        approval_id=str(r[2]),
        proposal_id=str(r[3]),
        payload_sha256=str(r[4]),
        precondition_sha256=str(r[5]) if r[5] is not None else None,
        issued_at=r[6],
        expires_at=r[7],
    )


def mint(
    cur: psycopg.Cursor[Any],
    tenant_id: str,
    *,
    approval_id: str,
    proposal_id: str,
    payload_sha256: str,
    precondition_sha256: str | None,
    now: datetime | None = None,
    ttl: timedelta | None = None,
) -> str:
    """One grant for one approve decision; call inside the decision's transaction."""
    grant_id = str(ULID())
    issued = now or datetime.now(UTC)
    cur.execute(
        f"INSERT INTO execution_grants ({_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            grant_id,
            tenant_id,
            approval_id,
            proposal_id,
            payload_sha256,
            precondition_sha256,
            issued,
            issued + (ttl or GRANT_TTL),
        ),
    )
    return grant_id


def for_approval(db: ScopedDB, approval_id: str) -> Grant | None:
    row = db.fetch_one(
        f"SELECT {_COLUMNS} FROM execution_grants WHERE approval_id = %s", (approval_id,)
    )
    return _grant(row) if row else None


def get(db: ScopedDB, grant_id: str) -> Grant | None:
    row = db.fetch_one(f"SELECT {_COLUMNS} FROM execution_grants WHERE grant_id = %s", (grant_id,))
    return _grant(row) if row else None


def for_proposal(db: ScopedDB, proposal_id: str) -> list[Grant]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM execution_grants WHERE proposal_id = %s"
        " ORDER BY issued_at, grant_id",
        (proposal_id,),
    )
    return [_grant(r) for r in rows]


def use_of(db: ScopedDB, grant_id: str) -> Use | None:
    row = db.fetch_one(
        "SELECT grant_id, operation, used_at FROM grant_uses WHERE grant_id = %s", (grant_id,)
    )
    return Use(str(row[0]), str(row[1]), row[2]) if row else None


def consume(cur: psycopg.Cursor[Any], tenant_id: str, grant: Grant, operation: str) -> None:
    """Spend the grant inside the caller's transaction. Of two callers racing, the second
    waits for the first to commit, then finds the key taken and raises GrantUsed."""
    row = cur.execute(
        "INSERT INTO grant_uses (tenant_id, grant_id, operation) VALUES (%s, %s, %s)"
        " ON CONFLICT (tenant_id, grant_id) DO NOTHING RETURNING grant_id",
        (tenant_id, grant.grant_id, operation),
    ).fetchone()
    if row is None:
        raise GrantUsed(f"grant {grant.grant_id} was used already; a new decision is needed")
