"""Roles per tenant (spec.md A-129): who may do what, decided by CCP, never by a token.

A person is a member of a tenant with one role, recorded as versions in `tenant_members`
(append-only, under row-level security), so the history says who held which role when
and who granted it. The roles are ranked: a check for a role passes for that role or any
above it.

    viewer     reads the tenant's state, evidence and runs
    reviewer   asks for changes on a proposal, comments
    approver   approves or rejects a proposal (never one they requested, A-130)
    admin      manages members and connections, and grants break-glass
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ulid import ULID

from ccp.auth.tokens import Principal
from ccp.storage.scoped import ScopedDB

Role = Literal["viewer", "reviewer", "approver", "admin"]
ROLES: tuple[Role, ...] = ("viewer", "reviewer", "approver", "admin")
OPERATOR = "operator"  # a member granted from the command line on the server itself


class NotPermitted(PermissionError):
    """The principal is not a member of this tenant, or its role is too low."""


@dataclass(frozen=True)
class Member:
    issuer: str
    subject: str
    email: str | None
    name: str | None
    role: Role
    active: bool
    granted_by: str
    recorded_at: datetime

    @property
    def principal(self) -> Principal:
        return Principal(self.issuer, self.subject, self.email, self.name)


_WRITTEN = "issuer, subject, email, name, role, active, granted_by"
_COLUMNS = _WRITTEN + ", recorded_at"


def _member(r: tuple[object, ...]) -> Member:
    role = str(r[4])
    assert role in ROLES
    return Member(
        issuer=str(r[0]),
        subject=str(r[1]),
        email=str(r[2]) if r[2] else None,
        name=str(r[3]) if r[3] else None,
        role=role,
        active=bool(r[5]),
        granted_by=str(r[6]),
        recorded_at=r[7],  # type: ignore[arg-type]
    )


def _append(db: ScopedDB, who: Principal, role: Role, active: bool, by: str) -> Member:
    with db.transaction() as cur:
        row = cur.execute(
            f"INSERT INTO tenant_members (tenant_id, member_id, {_WRITTEN})"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {_COLUMNS}",
            (
                db.tenant.tenant_id,
                str(ULID()),
                who.issuer,
                who.subject,
                who.email,
                who.name,
                role,
                active,
                by,
            ),
        ).fetchone()
    assert row is not None
    return _member(tuple(row))


def grant(db: ScopedDB, who: Principal, role: Role, granted_by: str) -> Member:
    """Make `who` a member with `role` (a new version, never an edit)."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; one of {ROLES}")
    if not granted_by.strip():
        raise ValueError("a grant names who made it")
    return _append(db, who, role, True, granted_by)


def revoke(db: ScopedDB, who: Principal, revoked_by: str) -> Member:
    """End `who`'s membership (a new version that is not active)."""
    found = member(db, who)
    if found is None or not found.active:
        raise LookupError(f"{who.label} is not a member of {db.tenant.tenant_id}")
    return _append(db, who, found.role, False, revoked_by)


def member(db: ScopedDB, who: Principal) -> Member | None:
    row = db.fetch_one(
        f"SELECT {_COLUMNS} FROM current_tenant_members WHERE issuer = %s AND subject = %s",
        (who.issuer, who.subject),
    )
    return _member(tuple(row)) if row else None


def current(db: ScopedDB) -> list[Member]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM current_tenant_members WHERE active ORDER BY role, issuer, subject"
    )
    return [_member(tuple(r)) for r in rows]


def role_of(db: ScopedDB, who: Principal) -> Role | None:
    found = member(db, who)
    return found.role if found is not None and found.active else None


def require(db: ScopedDB, who: Principal, at_least: Role) -> Role:
    """The principal's role in this tenant, if it is `at_least` the one asked for."""
    role = role_of(db, who)
    if role is None:
        raise NotPermitted(f"{who.label} is not a member of {db.tenant.tenant_id}")
    if ROLES.index(role) < ROLES.index(at_least):
        raise NotPermitted(
            f"{who.label} is a {role} of {db.tenant.tenant_id}; this needs {at_least}"
        )
    return role
