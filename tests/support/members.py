"""Signed-in members for tests (A-130): every decision is made by one."""

from __future__ import annotations

from ccp.auth import members
from ccp.auth.dev_issuer import DEV_ISSUER
from ccp.auth.tokens import Principal
from ccp.storage.scoped import ScopedDB


def member(db: ScopedDB, name: str = "auditor", role: members.Role = "approver") -> Principal:
    """A dev-issuer principal called `name`, holding `role` in the db's tenant. Its label is
    the name, as the typed reviewer names it replaces."""
    who = Principal(DEV_ISSUER, name, None, name)
    if members.role_of(db, who) != role:
        members.grant(db, who, role, members.OPERATOR)
    return who
