"""The auditor workspace: time-boxed, read-only access to named audit packages
(spec.md A-169).

A tenant admin grants one auditor (a principal at an identity provider, who need not be a
member of the tenant) read access to named packages until a time, at most 90 days ahead.
Granting is an assertion outside CCP, so it is judged under the publication policy and
recorded as an attestation first; a grant the policy blocks is not made. While a grant is
live and not revoked, the auditor can list those packages and read their files, and every
file read is logged. Nothing in the workspace writes anything but that log.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ulid import ULID

from ccp.compliance_state import attest
from ccp.storage.scoped import ScopedDB

MAX_DAYS = 90


@dataclass(frozen=True)
class Grant:
    grant_id: str
    principal_id: str
    package_ids: list[str]
    expires_at: datetime
    granted_by: str
    revoked: bool


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _package(db: ScopedDB, package_id: str) -> dict[str, Any] | None:
    row = db.fetch_one(
        "SELECT package_id, framework_id, valid_at, known_at, manifest, sealed_refs FROM"
        " audit_packages WHERE package_id = %s",
        (package_id,),
    )
    if row is None:
        return None
    return {
        "package_id": row[0],
        "framework_id": row[1],
        "valid_at": row[2],
        "known_at": row[3],
        "manifest": _load(row[4]),
        "sealed_refs": _load(row[5]),
    }


def grant(
    db: ScopedDB,
    principal_id: str,
    package_ids: list[str],
    days: int,
    by: str,
    reader: Any,
    note: str = "",
    now: datetime | None = None,
) -> tuple[Grant | None, attest.Decision]:
    """Judge the share under the policy, record it, and grant only what it allows. The
    items judged are the results in each package's sealed copy, read through `reader`."""
    if not 0 < days <= MAX_DAYS:
        raise ValueError(f"a grant lasts 1 to {MAX_DAYS} days")
    moment = now or datetime.now(UTC)
    items: list[attest.Item] = []
    for package_id in package_ids:
        found = _package(db, package_id)
        if found is None:
            raise ValueError(f"no package {package_id!r}")
        results = json.loads(reader.get(found["sealed_refs"]["results.json"]))
        items.extend(
            attest.Item(
                f"{package_id}:{r['subject_key']}",
                attest.provenance_of_result(r),
                r["status"] in ("pass", "fail"),
                datetime.fromisoformat(r["valid_from"]),
            )
            for r in results
        )
    decision = attest.attest(
        db,
        "package_share",
        f"auditor:{principal_id}",
        items,
        {"principal": principal_id, "packages": package_ids},
        by,
        moment,
    )
    if not decision.allowed:
        return None, decision
    grant_id = str(ULID())
    expires = moment + timedelta(days=days)
    db.execute(
        "INSERT INTO auditor_grants (tenant_id, grant_id, principal_id, package_ids, expires_at,"
        " granted_by, note) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (db.tenant.tenant_id, grant_id, principal_id, json.dumps(package_ids), expires, by, note),
    )
    return Grant(grant_id, principal_id, package_ids, expires, by, False), decision


def revoke(db: ScopedDB, grant_id: str, by: str, reason: str) -> None:
    if not reason.strip():
        raise ValueError("a revocation says why")
    db.execute(
        "INSERT INTO auditor_revocations (tenant_id, grant_id, revoked_by, reason)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (db.tenant.tenant_id, grant_id, by, reason),
    )


def live_grants(db: ScopedDB, principal_id: str, now: datetime | None = None) -> list[Grant]:
    moment = now or datetime.now(UTC)
    rows = db.fetch_all(
        "SELECT g.grant_id, g.principal_id, g.package_ids, g.expires_at, g.granted_by"
        " FROM auditor_grants g LEFT JOIN auditor_revocations r"
        " ON r.tenant_id = g.tenant_id AND r.grant_id = g.grant_id"
        " WHERE g.principal_id = %s AND g.expires_at > %s AND r.grant_id IS NULL",
        (principal_id, moment),
    )
    return [
        Grant(r[0], r[1], json.loads(r[2]) if isinstance(r[2], str) else r[2], r[3], r[4], False)
        for r in rows
    ]


def visible(db: ScopedDB, principal_id: str, now: datetime | None = None) -> dict[str, str]:
    """The packages this principal may read now, each with the grant that allows it."""
    out: dict[str, str] = {}
    for g in live_grants(db, principal_id, now):
        for package_id in g.package_ids:
            out.setdefault(package_id, g.grant_id)
    return out


def read_file(
    db: ScopedDB,
    principal_id: str,
    package_id: str,
    name: str,
    reader: Any,
    now: datetime | None = None,
) -> bytes:
    """One file of a package the principal may read, from its sealed copy; logged."""
    allowed = visible(db, principal_id, now)
    if package_id not in allowed:
        raise PermissionError("no live grant covers this package")
    found = _package(db, package_id)
    if found is None or name not in found["sealed_refs"]:
        raise FileNotFoundError(name)
    body: bytes = reader.get(found["sealed_refs"][name])
    db.execute(
        "INSERT INTO auditor_access (tenant_id, access_id, grant_id, principal_id, package_id,"
        " file) VALUES (%s, %s, %s, %s, %s, %s)",
        (db.tenant.tenant_id, str(ULID()), allowed[package_id], principal_id, package_id, name),
    )
    return body


def package_summary(db: ScopedDB, package_id: str) -> dict[str, Any] | None:
    found = _package(db, package_id)
    if found is None:
        return None
    return {
        "package_id": found["package_id"],
        "framework_id": found["framework_id"],
        "valid_at": found["valid_at"],
        "known_at": found["known_at"],
        "files": sorted(found["sealed_refs"]),
    }
