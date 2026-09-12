"""Tenant identity, typed.

``TenantId`` is a ``NewType`` so a bare string cannot be passed where a tenant is
expected without an explicit conversion, and ``TenantContext`` is what every function
that touches tenant data takes. ``ScopedDB`` turns the context into the database
setting that row-level security reads (spec.md Layer 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType

from ccp.config import Settings, TenantSettings

TenantId = NewType("TenantId", str)


class UnknownTenant(LookupError):
    """Raised when a tenant id is not in the tenants file."""


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: TenantId
    settings: TenantSettings

    @classmethod
    def for_id(cls, settings: Settings, tenant_id: str) -> TenantContext:
        tenants = settings.load_tenants()
        try:
            return cls(TenantId(tenant_id), tenants[tenant_id])
        except KeyError:
            raise UnknownTenant(
                f"unknown tenant {tenant_id!r}; known tenants: {sorted(tenants)}"
            ) from None

    @classmethod
    def all(cls, settings: Settings) -> list[TenantContext]:
        return [cls(TenantId(tid), ts) for tid, ts in sorted(settings.load_tenants().items())]
