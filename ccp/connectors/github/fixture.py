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
from datetime import UTC, datetime
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


FixtureDocs = dict[str, Any]  # org, members, repos, branch_protection, security_policies, ...


def load_docs(root: Path) -> FixtureDocs:
    if not root.is_dir():
        raise FileNotFoundError(f"no GitHub fixture at {root}")
    return {
        "org": load_json(root / "org.json"),
        "members": load_json(root / "members.json"),
        "repos": load_json(root / "repos.json"),
        "branch_protection": load_json(root / "branch_protection.json"),
        "security_policies": load_json(root / "security_policies.json"),
        "file_listings": load_json(root / "file_listings.json"),
        "deliveries": load_json(root / "deliveries.json"),
    }


class GitHubFixtureConnector:
    name: ConnectorName = "github"

    def __init__(self, fixtures_dir: Path) -> None:
        self._fixtures_dir = fixtures_dir

    def pull(self, tenant: TenantContext, since: datetime | None = None) -> Iterator[SourceEvent]:
        return after(self._events(tenant), since)

    def docs_for(self, tenant: TenantContext) -> FixtureDocs:
        root = self._fixtures_dir / tenant.tenant_id / "github"
        if not root.is_dir():
            raise FileNotFoundError(f"no GitHub fixture for tenant {tenant.tenant_id!r} at {root}")
        return load_docs(root)

    def _events(self, tenant: TenantContext) -> Iterator[SourceEvent]:
        docs = self.docs_for(tenant)

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

        org = docs["org"]
        yield event("org.snapshot", f"{org['node_id']}@{org['updated_at']}", org["updated_at"], org)

        for member in docs["members"]:
            yield event(
                "org.member.snapshot",
                f"{member['node_id']}@{member['updated_at']}",
                member["updated_at"],
                {"org": org["login"], **member},
            )

        protections = docs["branch_protection"]
        policies = docs["security_policies"]
        listings = docs["file_listings"]
        for repo in docs["repos"]:
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

        for delivery in docs["deliveries"]:
            event_type = _DELIVERY_TYPES[(delivery["event"], delivery["action"])]
            yield event(event_type, delivery["id"], delivery["delivered_at"], delivery)


# -- test-only: the simulated remediation loop (spec.md Layer 10 "Re-evaluate") -------------


class OverlayGitHubFixtureConnector(GitHubFixtureConnector):
    """A fixture connector over an in-memory copy of one tenant's documents. Unreachable from
    the CLI: only `apply_proposal` builds it, and only tests call that."""

    def __init__(self, fixtures_dir: Path, tenant_id: str, docs: FixtureDocs) -> None:
        super().__init__(fixtures_dir)
        self._overlay_tenant = tenant_id
        self._docs = docs

    def docs_for(self, tenant: TenantContext) -> FixtureDocs:
        if tenant.tenant_id == self._overlay_tenant:
            return self._docs
        return super().docs_for(tenant)


def apply_proposal(
    base: GitHubFixtureConnector,
    tenant: TenantContext,
    action: str,
    target: dict[str, str],
    at: datetime,
    delivery_id: str,
) -> OverlayGitHubFixtureConnector:
    """TEST ONLY. Mutate an in-memory copy of the fixture as GitHub would after the action:
    the collaborator (or member) disappears from the next snapshot, whose updated_at is
    bumped, and a removal delivery carries the revocation with its own timestamp."""
    import copy

    source = (
        base._docs if isinstance(base, OverlayGitHubFixtureConnector) else base.docs_for(tenant)
    )
    docs = copy.deepcopy(source)
    stamp = at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    login = target["login"]
    org = docs["org"]["login"]
    if action == "remove_repo_collaborator":
        repo_name = target["repo"].split("/", 1)[-1]
        repo = next((r for r in docs["repos"] if r["name"] == repo_name), None)
        if repo is None:
            raise LookupError(f"no repo {repo_name!r} in the fixture")
        user = next((c for c in repo["collaborators"] if c["login"] == login), None)
        if user is None:
            raise LookupError(f"{login} is not a collaborator on {repo_name}")
        repo["collaborators"] = [c for c in repo["collaborators"] if c["login"] != login]
        repo["updated_at"] = stamp
        docs["deliveries"].append(
            {
                "id": delivery_id,
                "event": "member",
                "action": "removed",
                "delivered_at": stamp,
                "payload": {
                    "repository": {
                        "name": repo_name,
                        "node_id": repo["node_id"],
                        "full_name": repo["full_name"],
                    },
                    "member": {
                        "login": login,
                        "node_id": user["node_id"],
                        "type": user.get("type", "User"),
                    },
                },
            }
        )
    elif action == "remove_org_member":
        member = next((m for m in docs["members"] if m["login"] == login), None)
        if member is None:
            raise LookupError(f"{login} is not a member of {org}")
        docs["members"] = [m for m in docs["members"] if m["login"] != login]
        docs["deliveries"].append(
            {
                "id": delivery_id,
                "event": "organization",
                "action": "member_removed",
                "delivered_at": stamp,
                "payload": {
                    "organization": {"login": org},
                    "membership": {
                        "role": member.get("role", "member"),
                        "user": {
                            "login": login,
                            "node_id": member["node_id"],
                            "type": member.get("type", "User"),
                        },
                    },
                },
            }
        )
    else:
        raise ValueError(f"apply_proposal cannot simulate {action!r}")
    return OverlayGitHubFixtureConnector(base._fixtures_dir, tenant.tenant_id, docs)
