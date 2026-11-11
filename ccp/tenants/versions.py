"""Versions of a tenant's settings (spec.md A-133), appended, never edited."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg
from ulid import ULID

from ccp import tenancy
from ccp.config import TenantSettings
from ccp.storage.scoped import ScopedDB

Status = Literal["active", "suspended", "offboarded"]
YAML = "config/tenants.yaml"  # the version a tenant has before its first database one


@dataclass(frozen=True)
class Version:
    version_id: str
    tenant_id: str
    settings: TenantSettings
    status: str
    reason: str
    recorded_by: str
    approval_id: str | None
    recorded_at: datetime


_COLUMNS = "version_id, tenant_id, settings, status, reason, recorded_by, approval_id, recorded_at"


def _row(r: tuple[Any, ...]) -> Version:
    raw = json.loads(r[2]) if isinstance(r[2], str) else r[2]
    return Version(
        version_id=str(r[0]),
        tenant_id=str(r[1]),
        settings=TenantSettings.model_validate(raw),
        status=str(r[3]),
        reason=str(r[4]),
        recorded_by=str(r[5]),
        approval_id=str(r[6]) if r[6] is not None else None,
        recorded_at=r[7],
    )


def current(db: ScopedDB) -> Version | None:
    row = db.fetch_one(f"SELECT {_COLUMNS} FROM current_tenants")
    return _row(row) if row else None


def history(db: ScopedDB) -> list[Version]:
    rows = db.fetch_all(f"SELECT {_COLUMNS} FROM tenant_versions ORDER BY recorded_at, version_id")
    return [_row(r) for r in rows]


def effective(db: ScopedDB) -> tuple[TenantSettings, str]:
    """(the settings in force, the version they come from: a version id, or the file)."""
    found = current(db)
    if found is not None:
        return found.settings, found.version_id
    return db.tenant.settings, YAML


def record_in(
    cur: psycopg.Cursor[Any],
    tenant_id: str,
    settings: TenantSettings,
    *,
    status: Status = "active",
    reason: str,
    recorded_by: str,
    approval_id: str | None = None,
) -> str:
    """Append a version inside the caller's transaction; the caller forgets the cached one
    once it commits (`tenancy.forget`)."""
    version_id = str(ULID())
    cur.execute(
        "INSERT INTO tenant_versions (tenant_id, version_id, settings, status, reason,"
        " recorded_by, approval_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            tenant_id,
            version_id,
            json.dumps(settings.model_dump(mode="json")),
            status,
            reason,
            recorded_by,
            approval_id,
        ),
    )
    return version_id


def record(
    db: ScopedDB,
    settings: TenantSettings,
    *,
    status: Status = "active",
    reason: str,
    recorded_by: str,
) -> str:
    with db.transaction() as cur:
        version_id = record_in(
            cur,
            db.tenant.tenant_id,
            settings,
            status=status,
            reason=reason,
            recorded_by=recorded_by,
        )
    tenancy.forget(db.tenant.tenant_id)
    return version_id
