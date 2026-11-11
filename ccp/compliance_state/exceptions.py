"""Asking for, and ending, a control exception (spec.md A-132).

A person decides and code enforces. A member (reviewer or above) asks for an exception for
one failing subject: the reason, the owner, and the date it ends, at most a year out. The
request is an `exception` approval item whose payload names the failing verdict; another
member, an approver, decides it, never the one who asked. Approval mints the exception
(ccp.approvals.actions); the subject then reads "fail (excepted until D)" and stays a failure
in every count. An approver may end an exception early. Nothing here changes a verdict.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ulid import ULID

from ccp.approvals import store as approvals
from ccp.auth import members
from ccp.auth.tokens import Principal
from ccp.storage import bitemporal
from ccp.storage import exceptions as stored
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus

MAX_TERM = timedelta(days=365)
MIN_REASON = 20  # characters: an exception is accountable, so its reason says something


class NotExceptable(ValueError):
    """The subject is not failing, or the request is incomplete."""


def request(
    db: ScopedDB,
    who: Principal,
    *,
    test_id: str,
    subject: str,
    until: datetime,
    reason: str,
    owner: str,
    now: datetime | None = None,
) -> str:
    """Open an exception item for one failing subject; returns its approval id."""
    members.require(db, who, "reviewer")
    moment = now or datetime.now(UTC)
    subject_key = subject if "|" in subject else f"{test_id}|{subject}"
    if not subject_key.startswith(f"{test_id}|"):
        raise NotExceptable(f"{subject_key} is not a subject of {test_id}")
    head = bitemporal.current_state(db, subject_key)
    if head is None or head.status is not VerdictStatus.failed:
        found = head.status.value if head is not None else "not evaluated"
        raise NotExceptable(f"only a failing subject takes an exception; {subject_key} is {found}")
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    if until <= moment:
        raise NotExceptable("an exception ends in the future")
    if until > moment + MAX_TERM:
        raise NotExceptable(f"an exception lasts at most {MAX_TERM.days} days; ask again then")
    if len(reason.strip()) < MIN_REASON:
        raise NotExceptable(f"say why in at least {MIN_REASON} characters")
    if not owner.strip():
        raise NotExceptable("name the owner who answers for the exception")
    payload = {
        "test_id": test_id,
        "subject_key": subject_key,
        "verdict_id": head.verdict_id,
        "status": head.status.value,
        "until": until.isoformat(),
        "reason": reason.strip(),
        "owner": owner.strip(),
        "shows_as": stored.label("fail", until),
    }
    return approvals.open_item(
        db, "exception", str(ULID()), payload, opened_by=who.label, requested_by=who.principal_id
    )


def revoke(db: ScopedDB, who: Principal, exception_id: str, reason: str) -> None:
    """End an exception early; an approver's call, recorded with the reason."""
    members.require(db, who, "approver")
    found = stored.get(db, exception_id)
    if found is None:
        raise LookupError(f"no exception {exception_id!r}")
    if found.revoked_at is not None:
        raise NotExceptable(f"exception {exception_id} was revoked at {found.revoked_at}")
    if not reason.strip():
        raise NotExceptable("say why the exception ends early")
    stored.revoke(db, exception_id, who.principal_id, reason.strip())
