from __future__ import annotations

import dataclasses

import pytest

from ccp.config import Settings
from ccp.tenancy import TenantContext, TenantId, UnknownTenant


def test_two_tenants_load_from_the_tenants_file(base_settings: Settings) -> None:
    tenants = base_settings.load_tenants()
    assert sorted(tenants) == ["acme", "globex"]
    assert tenants["acme"].org_admin_limit == 3
    assert tenants["globex"].org_admin_limit == 2
    assert "tier0" in tenants["acme"].critical_asset_tags


def test_tenant_context_for_known_id(base_settings: Settings) -> None:
    ctx = TenantContext.for_id(base_settings, "acme")
    assert ctx.tenant_id == TenantId("acme")
    assert ctx.settings.display_name == "Acme Corp"


def test_tenant_context_rejects_unknown_id(base_settings: Settings) -> None:
    with pytest.raises(UnknownTenant, match="globex"):
        TenantContext.for_id(base_settings, "initech")


def test_tenant_context_is_immutable(base_settings: Settings) -> None:
    ctx = TenantContext.for_id(base_settings, "acme")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.tenant_id = TenantId("globex")  # type: ignore[misc]


def test_all_tenants_are_sorted(base_settings: Settings) -> None:
    assert [c.tenant_id for c in TenantContext.all(base_settings)] == ["acme", "globex"]


def test_model_overrides_default_to_tier3(base_settings: Settings) -> None:
    assert base_settings.investigator_model == base_settings.ccp_tier3_model
    assert base_settings.policy_model == base_settings.ccp_tier3_model
    assert base_settings.copilot_model == base_settings.ccp_tier3_model
