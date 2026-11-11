"""Onboarding a tenant without a deploy (spec.md A-133).

`create` writes the tenant into the directory, records its first version from a few
answers (the name, the GitHub organization) and safe defaults for the rest, and makes the
person who signed up its first admin. Everything after that, connections included, is a
change of settings: what decides a status goes through a scope_change approval.
"""

from __future__ import annotations

import re

from psycopg import errors as pg_errors
from psycopg_pool import ConnectionPool

from ccp import tenancy
from ccp.auth import members
from ccp.auth.tokens import Principal
from ccp.config import Settings, TenantSettings
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext, TenantId
from ccp.tenants import versions

TENANT_ID = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


class AlreadyExists(ValueError):
    """A tenant with this id exists already."""


def defaults(display_name: str, github_org: str, time_zone: str = "UTC") -> TenantSettings:
    """A new tenant's first settings: strict where a status depends on them."""
    return TenantSettings(
        display_name=display_name,
        github_org=github_org,
        org_admin_limit=2,
        critical_asset_tags=("critical",),
        termination_revocation_sla_hours=24,
        time_zone=time_zone,
        connections=(),  # none until an admin connects one (A-121)
    )


def create(
    settings: Settings,
    pool: ConnectionPool,
    tenant_id: str,
    *,
    display_name: str,
    github_org: str,
    admin: Principal,
    recorded_by: str = members.OPERATOR,
    time_zone: str = "UTC",
) -> TenantContext:
    if not TENANT_ID.match(tenant_id):
        raise ValueError("a tenant id is 2 to 31 lowercase letters, digits or dashes")
    if tenant_id in settings.load_tenants() or tenant_id in tenancy.directory(settings):
        raise AlreadyExists(f"tenant {tenant_id!r} exists already")
    first = defaults(display_name.strip(), github_org.strip(), time_zone)
    ctx = TenantContext(TenantId(tenant_id), first)
    db = ScopedDB(pool, ctx)
    try:
        with db.transaction() as cur:
            cur.execute("INSERT INTO tenant_directory (tenant_id) VALUES (%s)", (tenant_id,))
            versions.record_in(cur, tenant_id, first, reason="onboarded", recorded_by=recorded_by)
    except pg_errors.UniqueViolation as exc:
        raise AlreadyExists(f"tenant {tenant_id!r} exists already") from exc
    members.grant(db, admin, "admin", recorded_by)
    tenancy.forget(tenant_id)
    return ctx
