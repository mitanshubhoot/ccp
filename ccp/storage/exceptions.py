"""Control exceptions as stored (spec.md A-132): a failing subject accepted until a date.

An exception never changes a verdict or a state row; a failure stays a failure, shown as
"fail (excepted until D)" while the exception holds and plain "fail" once it lapses or is
revoked, so it is never read as a pass. Only an approved exception item mints one (the
approval service), and code enforces the date: every read here takes the moment it is for.
This module sits low so the agents' read-only tools can say the same as the operator's report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from ulid import ULID

from ccp.storage.scoped import ScopedDB


@dataclass(frozen=True)
class ControlException:
    exception_id: str
    approval_id: str
    test_id: str
    subject_key: str
    reason: str
    owner: str
    valid_until: datetime
    granted_by: str
    granted_at: datetime
    revoked_by: str | None = None
    revoke_reason: str | None = None
    revoked_at: datetime | None = None

    def holds(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        if self.revoked_at is not None and self.revoked_at <= moment:
            return False
        return self.granted_at <= moment < self.valid_until


_COLUMNS = (
    "exception_id, approval_id, test_id, subject_key, reason, owner, valid_until, granted_by,"
    " granted_at, revoked_by, revoke_reason, revoked_at"
)


def _row(r: tuple[Any, ...]) -> ControlException:
    return ControlException(
        exception_id=str(r[0]),
        approval_id=str(r[1]),
        test_id=str(r[2]),
        subject_key=str(r[3]),
        reason=str(r[4]),
        owner=str(r[5]),
        valid_until=r[6],
        granted_by=str(r[7]),
        granted_at=r[8],
        revoked_by=str(r[9]) if r[9] is not None else None,
        revoke_reason=str(r[10]) if r[10] is not None else None,
        revoked_at=r[11],
    )


def insert(
    cur: psycopg.Cursor[Any],
    tenant_id: str,
    *,
    approval_id: str,
    test_id: str,
    subject_key: str,
    reason: str,
    owner: str,
    valid_until: datetime,
    granted_by: str,
) -> str:
    """Mint one exception inside the approving decision's transaction."""
    exception_id = str(ULID())
    cur.execute(
        "INSERT INTO control_exceptions (exception_id, tenant_id, approval_id, test_id,"
        " subject_key, reason, owner, valid_until, granted_by)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            exception_id,
            tenant_id,
            approval_id,
            test_id,
            subject_key,
            reason,
            owner,
            valid_until,
            granted_by,
        ),
    )
    return exception_id


def revoke(db: ScopedDB, exception_id: str, revoked_by: str, reason: str) -> None:
    db.execute(
        "INSERT INTO exception_revocations (tenant_id, exception_id, revoked_by, reason)"
        " VALUES (%s, %s, %s, %s)",
        (db.tenant.tenant_id, exception_id, revoked_by, reason),
    )


def get(db: ScopedDB, exception_id: str) -> ControlException | None:
    row = db.fetch_one(
        f"SELECT {_COLUMNS} FROM exception_status WHERE exception_id = %s", (exception_id,)
    )
    return _row(row) if row else None


def all_exceptions(db: ScopedDB) -> list[ControlException]:
    rows = db.fetch_all(f"SELECT {_COLUMNS} FROM exception_status ORDER BY granted_at")
    return [_row(r) for r in rows]


def active(db: ScopedDB, now: datetime | None = None) -> dict[str, ControlException]:
    """Subject key -> the exception holding at `now`; of two, the one that lasts longer."""
    out: dict[str, ControlException] = {}
    for item in all_exceptions(db):
        if not item.holds(now):
            continue
        held = out.get(item.subject_key)
        if held is None or item.valid_until > held.valid_until:
            out[item.subject_key] = item
    return out


def excepted_until(db: ScopedDB, now: datetime | None = None) -> dict[str, datetime]:
    return {key: item.valid_until for key, item in active(db, now).items()}


def label(status: str, until: datetime | None) -> str:
    """How a status reads with its exception: only a failure is ever excepted."""
    if status == "fail" and until is not None:
        return f"fail (excepted until {until:%Y-%m-%d})"
    return status
