"""MT4 (A-133): tenants live in the database, versioned, and a change of scope is approved.

The defects: a tenant existed only in config/tenants.yaml, so onboarding meant a deploy;
its settings changed with no record of who changed them or why; and a change to what
decides a status (the admin limit, the revocation window, the GitHub organization, the
connections) took effect without anyone approving it.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ccp import tenancy
from ccp.approvals import actions
from ccp.approvals import store as approvals
from ccp.auth import members
from ccp.auth.dev_issuer import DEV_ISSUER
from ccp.auth.tokens import Principal
from ccp.config import Settings
from ccp.evaluators.rule_versions import rule_stamp
from ccp.knowledge import graph as knowledge
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from ccp.tenants import onboarding, scope, versions
from tests.support.members import member

pytestmark = pytest.mark.db


@pytest.fixture
def own(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, ConnectionPool]]:
    try:
        with scratch_database(base_settings, prefix="ccp_tenants") as settings:
            pool = app_pool(settings, max_size=4)
            try:
                tenancy.forget()
                yield settings, ScopedDB(pool, TenantContext.for_id(settings, "acme")), pool
            finally:
                pool.close()
                tenancy.forget()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


def test_a_tenant_onboards_without_a_deploy(own: tuple[Settings, ScopedDB, ConnectionPool]) -> None:
    settings, acme, pool = own
    founder = Principal(DEV_ISSUER, "founder")
    ctx = onboarding.create(
        settings,
        pool,
        "initech",
        display_name="Initech",
        github_org="initech-example",
        admin=founder,
    )
    assert "initech" not in settings.load_tenants()  # not in the file: in the database
    assert TenantContext.for_id(settings, "initech").settings.github_org == "initech-example"
    assert "initech" in [c.tenant_id for c in TenantContext.all(settings)]
    initech = ScopedDB(pool, ctx)
    assert members.role_of(initech, founder) == "admin"
    assert members.role_of(acme, founder) is None  # nothing in another tenant
    first = versions.current(initech)
    assert first is not None and first.reason == "onboarded" and first.status == "active"
    with pytest.raises(onboarding.AlreadyExists):
        onboarding.create(
            settings, pool, "initech", display_name="x", github_org="x", admin=founder
        )
    with pytest.raises(onboarding.AlreadyExists):  # the file's tenants are taken too
        onboarding.create(settings, pool, "acme", display_name="x", github_org="x", admin=founder)
    with pytest.raises(ValueError, match="lowercase"):
        onboarding.create(settings, pool, "Bad Id", display_name="x", github_org="x", admin=founder)


def test_the_file_holds_until_a_tenant_has_a_version(
    own: tuple[Settings, ScopedDB, ConnectionPool],
) -> None:
    settings, acme, pool = own
    assert versions.current(acme) is None
    current, version = versions.effective(acme)
    assert version == versions.YAML and current == settings.load_tenants()["acme"]
    admin = member(acme, "ada", "admin")
    recorded, item = scope.change(acme, admin, {"display_name": "Acme Corporation"})
    assert item is None and recorded is not None  # not a change of scope: recorded at once
    assert TenantContext.for_id(settings, "acme").settings.display_name == "Acme Corporation"
    with pytest.raises(members.NotPermitted):
        scope.change(acme, member(acme, "ana"), {"display_name": "Acme"})  # admins only


def test_a_change_of_scope_waits_for_an_approver(
    own: tuple[Settings, ScopedDB, ConnectionPool],
) -> None:
    settings, acme, pool = own
    admin = member(acme, "ada", "admin")
    spec = knowledge.load().tests["GH-2"]
    before = rule_stamp(spec, TenantContext.for_id(settings, "acme").settings)
    recorded, item = scope.change(acme, admin, {"org_admin_limit": 5})
    assert recorded is None and item is not None
    asked = approvals.get(acme, item)
    assert asked is not None and asked.kind == "scope_change"
    assert asked.payload["changes"] == {"org_admin_limit": {"from": 3, "to": 5}}
    assert "GH-2" in asked.payload["affects"]
    assert TenantContext.for_id(settings, "acme").settings.org_admin_limit == 3  # not yet
    with pytest.raises(members.NotPermitted, match="asked for this"):
        actions.approve(acme, settings, item, admin)
    actions.approve(acme, settings, item, member(acme, "ana"))
    now = versions.current(acme)
    assert now is not None and now.approval_id == item and now.settings.org_admin_limit == 5
    assert TenantContext.for_id(settings, "acme").settings.org_admin_limit == 5
    assert rule_stamp(spec, TenantContext.for_id(settings, "acme").settings) != before


def test_a_change_asked_against_old_settings_is_refused(
    own: tuple[Settings, ScopedDB, ConnectionPool],
) -> None:
    settings, acme, pool = own
    admin = member(acme, "ada", "admin")
    _, first = scope.change(acme, admin, {"org_admin_limit": 4})
    _, second = scope.change(acme, admin, {"termination_revocation_sla_hours": 48})
    assert first is not None and second is not None
    actions.approve(acme, settings, first, member(acme, "ana"))
    with pytest.raises(scope.Superseded, match="ask again"):
        actions.approve(acme, settings, second, member(acme, "ana"))
    still = approvals.get(acme, second)
    assert still is not None and still.is_open  # nothing was recorded
    assert TenantContext.for_id(settings, "acme").settings.termination_revocation_sla_hours == 24


def test_a_suspended_tenant_runs_nothing(own: tuple[Settings, ScopedDB, ConnectionPool]) -> None:
    settings, acme, pool = own
    current, _ = versions.effective(acme)
    versions.record(acme, current, status="suspended", reason="unpaid", recorded_by="operator")
    with pytest.raises(tenancy.TenantUnavailable, match="suspended"):
        TenantContext.for_id(settings, "acme")
    assert "acme" not in [c.tenant_id for c in TenantContext.all(settings)]
