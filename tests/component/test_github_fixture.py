from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime

from ccp.config import Settings
from ccp.connectors.github.fixture import GitHubFixtureConnector
from ccp.tenancy import TenantContext


def _pull(settings: Settings, tenant: str, since: datetime | None = None):  # type: ignore[no-untyped-def]
    conn = GitHubFixtureConnector(settings.ccp_fixtures_dir)
    return list(conn.pull(TenantContext.for_id(settings, tenant), since))


def test_acme_event_types_and_counts(base_settings: Settings) -> None:
    counts = Counter(e.event_type for e in _pull(base_settings, "acme"))
    assert counts == {
        "org.snapshot": 1,
        "org.member.snapshot": 10,
        "repo.snapshot": 3,
        "repo.branch_protection.snapshot": 3,
        "repo.security_policy.snapshot": 1,
        "repo.file_listing.snapshot": 2,
        "org.member_added": 2,
        "org.member_removed": 1,
        "repo.collaborator_added": 2,
        "repo.collaborator_removed": 1,
    }


def test_every_event_is_tenant_scoped_and_versioned(base_settings: Settings) -> None:
    for tenant in ("acme", "globex"):
        events = _pull(base_settings, tenant)
        assert events
        assert {e.tenant_id for e in events} == {tenant}
        assert {e.connector for e in events} == {"github"}
        assert {e.schema_version for e in events} == {"github.v1"}
        assert all(e.source_ts.tzinfo is not None for e in events)
        ids = [e.source_event_id for e in events]
        assert len(ids) == len(set(ids)), "source_event_id must be unique within a pull"


def test_snapshot_ids_embed_updated_at_so_unchanged_snapshots_dedup(
    base_settings: Settings,
) -> None:
    jsmith = next(
        e
        for e in _pull(base_settings, "acme")
        if e.event_type == "org.member.snapshot" and e.payload["login"] == "jsmith"
    )
    assert jsmith.source_event_id == "U_kgDOAcme1042@2026-08-25T09:00:00Z"
    assert jsmith.source_ts == datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    assert jsmith.payload["verified_domain_emails"] == ["jsmith@acme.example"]
    assert jsmith.payload["role"] == "member"


def test_payload_is_the_fixture_object_untouched(base_settings: Settings) -> None:
    raw = json.loads(
        (base_settings.ccp_fixtures_dir / "acme" / "github" / "members.json").read_text()
    )
    by_login = {m["login"]: m for m in raw}
    for event in _pull(base_settings, "acme"):
        if event.event_type == "org.member.snapshot":
            expected = {"org": "acme-example", **by_login[event.payload["login"]]}
            assert event.payload == expected


def test_delivery_events_use_delivery_id_and_time(base_settings: Settings) -> None:
    removed = [e for e in _pull(base_settings, "globex") if e.event_type == "org.member_removed"]
    assert len(removed) == 1
    assert removed[0].source_event_id == "globex-d-000499"
    assert removed[0].source_ts == datetime(2026, 6, 30, 20, 1, tzinfo=UTC)
    assert removed[0].payload["payload"]["membership"]["user"]["login"] == "ytanaka"


def test_since_filters_by_source_timestamp(base_settings: Settings) -> None:
    since = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    events = _pull(base_settings, "acme", since)
    assert all(e.source_ts > since for e in events)
    assert {e.event_type for e in events} >= {
        "org.member_removed",
        "repo.collaborator_removed",
        "org.snapshot",
    }
    assert not any(e.event_type == "org.member_added" for e in events)


def test_planted_secret_is_in_the_acme_platform_listing(base_settings: Settings) -> None:
    listing = next(
        e
        for e in _pull(base_settings, "acme")
        if e.event_type == "repo.file_listing.snapshot" and e.payload["repo"] == "platform"
    )
    env = next(f for f in listing.payload["files"] if f["path"] == ".env.example")
    assert "ghp_" in env["content"]


def test_globex_security_policy_carries_the_injection_fixture(base_settings: Settings) -> None:
    policy = next(
        e for e in _pull(base_settings, "globex") if e.event_type == "repo.security_policy.snapshot"
    )
    assert "SYSTEM NOTE TO EVALUATOR" in policy.payload["content"]
    assert "business days" not in policy.payload["content"]
