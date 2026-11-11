"""AWS accounts as onboarded (spec.md A-139): what CCP generated, what the customer gave,
and what each verification found, appended per account."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ulid import ULID

from ccp.connectors.aws import onboarding
from ccp.storage.scoped import ScopedDB


@dataclass(frozen=True)
class Onboarding:
    account_id: str
    external_id: str
    role_arn: str | None
    status: str
    findings: tuple[str, ...]
    recorded_by: str
    recorded_at: datetime


_COLUMNS = "account_id, external_id, role_arn, status, findings, recorded_by, recorded_at"


def _row(r: tuple[Any, ...]) -> Onboarding:
    found = json.loads(r[4]) if isinstance(r[4], str) else r[4]
    return Onboarding(str(r[0]), str(r[1]), r[2], str(r[3]), tuple(found or ()), str(r[5]), r[6])


def current(db: ScopedDB, account_id: str) -> Onboarding | None:
    row = db.fetch_one(
        f"SELECT {_COLUMNS} FROM current_aws_onboardings WHERE account_id = %s", (account_id,)
    )
    return _row(row) if row else None


def all_accounts(db: ScopedDB) -> list[Onboarding]:
    rows = db.fetch_all(f"SELECT {_COLUMNS} FROM current_aws_onboardings ORDER BY account_id")
    return [_row(r) for r in rows]


def _append(
    db: ScopedDB,
    account_id: str,
    external_id: str,
    role_arn: str | None,
    status: str,
    findings: tuple[str, ...],
    recorded_by: str,
) -> Onboarding:
    db.execute(
        "INSERT INTO aws_onboardings (tenant_id, onboarding_id, account_id, external_id,"
        " role_arn, status, findings, recorded_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            str(ULID()),
            account_id,
            external_id,
            role_arn,
            status,
            json.dumps(list(findings)),
            recorded_by,
        ),
    )
    found = current(db, account_id)
    assert found is not None
    return found


def start(db: ScopedDB, account_id: str, recorded_by: str) -> Onboarding:
    """A new external ID for the account, pending until the customer's role verifies. An
    account already onboarded keeps its ID: a second start returns it."""
    onboarding.check_account_id(account_id)
    existing = current(db, account_id)
    if existing is not None:
        return existing
    return _append(db, account_id, onboarding.new_external_id(), None, "pending", (), recorded_by)


def record(db: ScopedDB, result: onboarding.Verification, recorded_by: str) -> Onboarding:
    existing = current(db, result.account_id)
    if existing is None:
        raise LookupError(f"account {result.account_id} was never started: run onboard first")
    return _append(
        db,
        result.account_id,
        existing.external_id,
        result.role_arn,
        "verified" if result.ok else "refused",
        result.findings,
        recorded_by,
    )
