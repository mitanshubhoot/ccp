"""MT4 (A-129): roles per tenant, as versions under row-level security."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from ccp.auth import members
from ccp.auth.tokens import Principal
from ccp.config import Settings
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

pytestmark = pytest.mark.db

ANA = Principal("https://idp.acme.example", "ana-1", "ana@acme.example", "Ana")
BO = Principal("https://idp.acme.example", "bo-2", "bo@acme.example", "Bo")


@pytest.fixture
def tenants(base_settings: Settings) -> Iterator[tuple[ScopedDB, ScopedDB]]:
    try:
        with scratch_database(base_settings, prefix="ccp_members") as settings:
            pool = app_pool(settings, max_size=2)
            try:
                yield (
                    ScopedDB(pool, TenantContext.for_id(settings, "acme")),
                    ScopedDB(pool, TenantContext.for_id(settings, "globex")),
                )
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


def test_a_role_is_granted_changed_and_revoked_as_versions(
    tenants: tuple[ScopedDB, ScopedDB],
) -> None:
    acme, _ = tenants
    members.grant(acme, ANA, "viewer", members.OPERATOR)
    assert members.role_of(acme, ANA) == "viewer"
    members.grant(acme, ANA, "approver", "https://idp.acme.example#admin-0")
    assert members.role_of(acme, ANA) == "approver"
    assert members.require(acme, ANA, "reviewer") == "approver"  # a higher role passes
    with pytest.raises(members.NotPermitted, match="this needs admin"):
        members.require(acme, ANA, "admin")
    members.revoke(acme, ANA, members.OPERATOR)
    assert members.role_of(acme, ANA) is None
    with pytest.raises(members.NotPermitted, match="not a member"):
        members.require(acme, ANA, "viewer")
    history = acme.fetch_all(
        "SELECT role, active, granted_by FROM tenant_members WHERE subject = 'ana-1'"
        " ORDER BY recorded_at"
    )
    assert [tuple(r) for r in history] == [
        ("viewer", True, "operator"),
        ("approver", True, "https://idp.acme.example#admin-0"),
        ("approver", False, "operator"),
    ]


def test_a_member_of_one_tenant_is_no_one_in_another(tenants: tuple[ScopedDB, ScopedDB]) -> None:
    acme, globex = tenants
    members.grant(acme, BO, "admin", members.OPERATOR)
    assert members.role_of(acme, BO) == "admin"
    assert members.role_of(globex, BO) is None
    assert members.current(globex) == []
    with pytest.raises(members.NotPermitted):
        members.require(globex, BO, "viewer")


def test_a_grant_names_a_known_role_and_who_made_it(tenants: tuple[ScopedDB, ScopedDB]) -> None:
    acme, _ = tenants
    with pytest.raises(ValueError, match="unknown role"):
        members.grant(acme, ANA, "owner", members.OPERATOR)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="who made it"):
        members.grant(acme, ANA, "viewer", " ")
    with pytest.raises(LookupError):
        members.revoke(acme, ANA, members.OPERATOR)
