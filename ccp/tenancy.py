"""Tenant identity, typed.

``TenantId`` is a ``NewType`` so a bare string cannot be passed where a tenant is
expected without an explicit conversion, and ``TenantContext`` is what every function
that touches tenant data takes. ``ScopedDB`` turns the context into the database
setting that row-level security reads (spec.md Layer 5).

Tenants live in the database (A-133). ``for_id`` reads a tenant's current version from
``current_tenants``, under that tenant's own row-level security, and falls back to
config/tenants.yaml for a tenant with no version yet: the fixture tenants, or a database
that is not migrated or not reachable. A suspended or offboarded tenant is refused. A read
is kept for a short while per database and tenant; whoever records a new version calls
``forget`` so the next read sees it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import NewType

import psycopg

from ccp.config import Settings, TenantSettings

TenantId = NewType("TenantId", str)

CACHE_SECONDS = 30.0
_versions: dict[tuple[str, str], tuple[float, TenantSettings | None, str | None]] = {}
_unreachable: dict[str, float] = {}


class UnknownTenant(LookupError):
    """Raised when a tenant id is neither in the database nor in the tenants file."""


class TenantUnavailable(LookupError):
    """The tenant is suspended or offboarded: nothing runs for it."""


def forget(tenant_id: str | None = None) -> None:
    """Drop what was read for a tenant (all tenants with None), after a new version."""
    for key in [k for k in _versions if tenant_id is None or k[1] == tenant_id]:
        _versions.pop(key, None)
    if tenant_id is None:
        _unreachable.clear()


def _stored(settings: Settings, tenant_id: str) -> tuple[TenantSettings | None, str | None]:
    """(settings, status) of the tenant's current version; (None, None) when it has none,
    or the database is not migrated that far, or it cannot be reached just now."""
    url = settings.database_url
    now = time.monotonic()
    if now - _unreachable.get(url, -CACHE_SECONDS * 2) < CACHE_SECONDS:
        return None, None
    try:
        with psycopg.connect(url, connect_timeout=2) as conn:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            # row security shows this tenant's version alone: no predicate of our own
            row = conn.execute("SELECT settings, status FROM current_tenants").fetchone()
    except psycopg.errors.UndefinedTable:
        return None, None
    except psycopg.OperationalError:
        _unreachable[url] = now
        return None, None
    if row is None:
        return None, None
    return TenantSettings.model_validate(row[0]), str(row[1])


def lookup(settings: Settings, tenant_id: str) -> TenantSettings:
    """A tenant's settings now: its current database version, else the tenants file."""
    key = (settings.database_url, tenant_id)
    now = time.monotonic()
    held = _versions.get(key)
    if held is None or now - held[0] > CACHE_SECONDS:
        stored, status = _stored(settings, tenant_id)
        held = (now, stored, status)
        _versions[key] = held
    _, stored, status = held
    if status is not None and status != "active":
        raise TenantUnavailable(f"tenant {tenant_id!r} is {status}; nothing runs for it")
    if stored is not None:
        return stored
    tenants = settings.load_tenants()
    try:
        return tenants[tenant_id]
    except KeyError:
        raise UnknownTenant(
            f"unknown tenant {tenant_id!r}; known tenants: {sorted(tenants)}"
        ) from None


def directory(settings: Settings) -> list[str]:
    """Tenants onboarded into the database (A-133); empty when it cannot be read."""
    try:
        with psycopg.connect(settings.database_url, connect_timeout=2) as conn:
            rows = conn.execute("SELECT tenant_id FROM tenant_directory").fetchall()
    except (psycopg.errors.UndefinedTable, psycopg.OperationalError):
        return []
    return sorted(str(r[0]) for r in rows)


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: TenantId
    settings: TenantSettings

    @classmethod
    def for_id(cls, settings: Settings, tenant_id: str) -> TenantContext:
        return cls(TenantId(tenant_id), lookup(settings, tenant_id))

    @classmethod
    def all(cls, settings: Settings) -> list[TenantContext]:
        """Every active tenant, from the tenants file and the database."""
        out: list[TenantContext] = []
        for tid in sorted(set(settings.load_tenants()) | set(directory(settings))):
            try:
                out.append(cls.for_id(settings, tid))
            except TenantUnavailable:
                continue
        return out
