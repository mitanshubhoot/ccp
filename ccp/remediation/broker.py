"""Credentials for the remediation executor, one per approved operation (spec.md A-116).

The executor never reads GITHUB_TOKEN: it asks here, naming the operation (the proposal),
the action, the approval item that allowed it and the scope it will touch. The broker checks
the approval itself, whatever the gates said before it, records the issue in the insert-only
`credential_issues` table, and returns a credential bound to that one operation and action.
GitHub's token is still a standing token, so the expiry is the record's, not the token's;
the GitHub App of LT1 makes the token itself one repository and one hour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ulid import ULID

from ccp import credentials
from ccp.approvals.store import Approval
from ccp.config import Settings
from ccp.storage.scoped import ScopedDB

TTL = timedelta(minutes=15)


class NotApproved(PermissionError):
    """No approved item stands behind the operation: no credential is issued."""


@dataclass(frozen=True)
class Credential:
    token: str = field(repr=False)  # never printed, never logged
    operation: str
    action: str
    scope: str
    issued_at: datetime
    expires_at: datetime


def issue(
    db: ScopedDB,
    settings: Settings,
    *,
    operation: str,
    action: str,
    approval: Approval | None,
    scope: str,
) -> Credential | None:
    """A credential for one approved operation, recorded; None when no token is configured.
    Raises NotApproved when the approval is missing, undecided, rejected or another's."""
    if approval is None or approval.state != "approved" or approval.ref_id != operation:
        raise NotApproved(f"no approved item stands behind {action} of {operation}")
    token = credentials.remediation_token(settings)
    if token is None:
        return None
    issued = datetime.now(UTC)
    db.execute(
        "INSERT INTO credential_issues (issue_id, tenant_id, operation, action, approval_id,"
        " payload_sha256, scope, issued_at, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            str(ULID()),
            db.tenant.tenant_id,
            operation,
            action,
            approval.approval_id,
            approval.payload_sha256,
            scope,
            issued,
            issued + TTL,
        ),
    )
    return Credential(token, operation, action, scope, issued, issued + TTL)
