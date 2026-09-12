"""Live GitHub client: runs only with GITHUB_TOKEN and network (spec.md D1, A-3).

The live client must produce the same event types and id shapes as the fixture reader,
so the same downstream expectations hold. Webhook deliveries are not available without
a webhook id; the live path emits snapshots only.
"""

from __future__ import annotations

import os

import pytest

from ccp.config import Settings
from ccp.connectors.registry import build_connector
from ccp.tenancy import TenantContext

pytestmark = pytest.mark.live_github


@pytest.mark.skipif(not os.environ.get("GITHUB_TOKEN"), reason="GITHUB_TOKEN not set")
def test_live_client_matches_fixture_event_shapes(base_settings: Settings) -> None:
    org = os.environ.get("CCP_LIVE_GITHUB_ORG")
    if not org:
        pytest.skip("CCP_LIVE_GITHUB_ORG not set; name a disposable org you own")
    tenant = TenantContext.for_id(base_settings, "acme")
    tenant = TenantContext(tenant.tenant_id, tenant.settings.model_copy(update={"github_org": org}))
    conn = build_connector("github", base_settings, "live")
    events = list(conn.pull(tenant))
    types = {e.event_type for e in events}
    assert {
        "org.snapshot",
        "org.member.snapshot",
        "repo.snapshot",
        "repo.branch_protection.snapshot",
    } <= types
    for event in events:
        assert event.connector == "github"
        assert event.schema_version == "github.v1"
        assert "@" in event.source_event_id
        assert event.source_ts.tzinfo is not None
        if event.event_type == "org.member.snapshot":
            assert set(event.payload) >= {
                "login",
                "node_id",
                "type",
                "role",
                "verified_domain_emails",
                "updated_at",
            }
