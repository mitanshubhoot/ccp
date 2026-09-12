"""GitHub fixture reader.

Reads ``fixtures/{tenant}/github/*.json`` and emits the same event shapes the live client
produces. Snapshot events carry ``source_event_id = f"{node_id}@{updated_at}"`` so an
unchanged snapshot dedups to nothing on re-pull; delivery events use the delivery id.

Event types:
  org.snapshot                    the organization object
  org.member.snapshot             one per member: login, node_id, type, role, verified_domain_emails
  repo.snapshot                   one per repo: settings and direct collaborators
  repo.branch_protection.snapshot one per repo: the default branch's protection (or protected=false)
  repo.security_policy.snapshot   one per repo that has a SECURITY.md
  repo.file_listing.snapshot      one per repo with a tracked-file listing (GH-5 reads this)
  org.member_added / org.member_removed / repo.collaborator_added / repo.collaborator_removed
                                  webhook deliveries, id = delivery id, ts = delivered_at
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ccp.connectors.base import ConnectorName, SourceEvent, after, load_json, parse_ts
from ccp.tenancy import TenantContext

SCHEMA_VERSION = "github.v1"

_DELIVERY_TYPES: dict[tuple[str, str], str] = {
    ("organization", "member_added"): "org.member_added",
    ("organization", "member_removed"): "org.member_removed",
    ("member", "added"): "repo.collaborator_added",
    ("member", "removed"): "repo.collaborator_removed",
}


class GitHubFixtureConnector:
    name: ConnectorName = "github"

    def __init__(self, fixtures_dir: Path) -> None:
        self._fixtures_dir = fixtures_dir

    def pull(self, tenant: TenantContext, since: datetime | None = None) -> Iterator[SourceEvent]:
        return after(self._events(tenant), since)

    def _events(self, tenant: TenantContext) -> Iterator[SourceEvent]:
        root = self._fixtures_dir / tenant.tenant_id / "github"
        if not root.is_dir():
            raise FileNotFoundError(f"no GitHub fixture for tenant {tenant.tenant_id!r} at {root}")

        def event(
            event_type: str, source_event_id: str, ts: str, payload: dict[str, Any]
        ) -> SourceEvent:
            return SourceEvent(
                tenant_id=tenant.tenant_id,
                connector="github",
                source_event_id=source_event_id,
                source_ts=parse_ts(ts),
                event_type=event_type,
                payload=payload,
                schema_version=SCHEMA_VERSION,
            )

        org = load_json(root / "org.json")
        yield event("org.snapshot", f"{org['node_id']}@{org['updated_at']}", org["updated_at"], org)

        for member in load_json(root / "members.json"):
            yield event(
                "org.member.snapshot",
                f"{member['node_id']}@{member['updated_at']}",
                member["updated_at"],
                {"org": org["login"], **member},
            )

        protections = load_json(root / "branch_protection.json")
        policies = load_json(root / "security_policies.json")
        listings = load_json(root / "file_listings.json")
        for repo in load_json(root / "repos.json"):
            name = repo["name"]
            node_id = repo["node_id"]
            yield event(
                "repo.snapshot",
                f"{node_id}@{repo['updated_at']}",
                repo["updated_at"],
                {"org": org["login"], **repo},
            )
            protection = protections.get(name)
            if protection is not None:
                yield event(
                    "repo.branch_protection.snapshot",
                    f"{node_id}:protection@{protection['updated_at']}",
                    protection["updated_at"],
                    {"org": org["login"], "repo": name, "repo_node_id": node_id, **protection},
                )
            policy = policies.get(name)
            if policy is not None:
                yield event(
                    "repo.security_policy.snapshot",
                    f"{node_id}:security@{policy['updated_at']}",
                    policy["updated_at"],
                    {"org": org["login"], "repo": name, "repo_node_id": node_id, **policy},
                )
            listing = listings.get(name)
            if listing is not None:
                yield event(
                    "repo.file_listing.snapshot",
                    f"{node_id}:files@{listing['updated_at']}",
                    listing["updated_at"],
                    {"org": org["login"], "repo": name, "repo_node_id": node_id, **listing},
                )

        for delivery in load_json(root / "deliveries.json"):
            event_type = _DELIVERY_TYPES[(delivery["event"], delivery["action"])]
            yield event(event_type, delivery["id"], delivery["delivered_at"], delivery)
