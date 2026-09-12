"""GitHub live client: the same seven reads as the fixture, against api.github.com.

Selected with ``--source live`` when ``GITHUB_TOKEN`` is set. Produces the same event
types and ids as the fixture reader so nothing downstream can tell them apart. Webhook
deliveries are not available without a webhook id, so the live client emits snapshots
only; that gap is documented in spec.md A-3 and covered by the ``live_github`` test.

Endpoints: GET /orgs/{org}; GET /orgs/{org}/members (role=admin and role=all);
GET /orgs/{org}/repos; GET /repos/{full}/collaborators?affiliation=direct;
GET /repos/{full}/branches/{default}/protection; GET /repos/{full}/contents/SECURITY.md;
GET /repos/{full}/contents (top-level files, small text files inlined);
POST /graphql for organizationVerifiedDomainEmails per member.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from ccp.connectors.base import ConnectorName, SourceEvent, after
from ccp.tenancy import TenantContext

SCHEMA_VERSION = "github.v1"
_MAX_INLINE_FILE_BYTES = 20_000
_VERIFIED_EMAILS_QUERY = """
query($login: String!, $org: String!) {
  user(login: $login) { organizationVerifiedDomainEmails(login: $org) }
}
"""


class GitHubLiveConnector:
    name: ConnectorName = "github"

    def __init__(self, token: str, base_url: str = "https://api.github.com") -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    def pull(self, tenant: TenantContext, since: datetime | None = None) -> Iterator[SourceEvent]:
        return after(self._events(tenant), since)

    # -- helpers -------------------------------------------------------------------

    def _get(self, path: str, **params: Any) -> Any:
        response = self._client.get(path, params=params or None)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def _paginate(self, path: str, **params: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._get(path, per_page=100, page=page, **params)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    def _verified_emails(self, login: str, org: str) -> list[str]:
        response = self._client.post(
            "/graphql",
            json={"query": _VERIFIED_EMAILS_QUERY, "variables": {"login": login, "org": org}},
        )
        if response.status_code != 200:
            return []
        data = response.json().get("data") or {}
        user = data.get("user") or {}
        emails = user.get("organizationVerifiedDomainEmails") or []
        return [e for e in emails if isinstance(e, str)]

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    # -- the pull ------------------------------------------------------------------

    def _events(self, tenant: TenantContext) -> Iterator[SourceEvent]:
        org = tenant.settings.github_org
        snapshot_ts = self._now()

        def event(
            event_type: str, source_event_id: str, ts: str, payload: dict[str, Any]
        ) -> SourceEvent:
            return SourceEvent(
                tenant_id=tenant.tenant_id,
                connector="github",
                source_event_id=source_event_id,
                source_ts=datetime.fromisoformat(ts.replace("Z", "+00:00")),
                event_type=event_type,
                payload=payload,
                schema_version=SCHEMA_VERSION,
            )

        org_obj = self._get(f"/orgs/{org}")
        if org_obj is None:
            raise LookupError(f"GitHub organization {org!r} not found or token lacks access")
        org_updated = org_obj.get("updated_at") or snapshot_ts
        yield event("org.snapshot", f"{org_obj['node_id']}@{org_updated}", org_updated, org_obj)

        admins = {m["login"] for m in self._paginate(f"/orgs/{org}/members", role="admin")}
        for member in self._paginate(f"/orgs/{org}/members", role="all"):
            payload = {
                "org": org,
                "login": member["login"],
                "node_id": member["node_id"],
                "type": member.get("type", "User"),
                "role": "admin" if member["login"] in admins else "member",
                "verified_domain_emails": self._verified_emails(member["login"], org),
                "name": None,
                "updated_at": snapshot_ts,
            }
            yield event(
                "org.member.snapshot", f"{member['node_id']}@{snapshot_ts}", snapshot_ts, payload
            )

        for repo in self._paginate(f"/orgs/{org}/repos", type="all"):
            full = repo["full_name"]
            node_id = repo["node_id"]
            updated = repo.get("updated_at") or snapshot_ts
            collaborators = [
                {
                    "login": c["login"],
                    "node_id": c["node_id"],
                    "type": c.get("type", "User"),
                    "permission": c.get("role_name") or c.get("permissions", {}),
                }
                for c in self._paginate(f"/repos/{full}/collaborators", affiliation="direct")
            ]
            repo_payload = {
                "org": org,
                "name": repo["name"],
                "node_id": node_id,
                "full_name": full,
                "private": repo.get("private", False),
                "default_branch": repo.get("default_branch", "main"),
                "updated_at": updated,
                "collaborators": collaborators,
            }
            yield event("repo.snapshot", f"{node_id}@{updated}", updated, repo_payload)

            protection = self._get(
                f"/repos/{full}/branches/{repo_payload['default_branch']}/protection"
            )
            protection_payload: dict[str, Any] = {
                "org": org,
                "repo": repo["name"],
                "repo_node_id": node_id,
                "branch": repo_payload["default_branch"],
                "protected": protection is not None,
                "updated_at": snapshot_ts,
            }
            if protection is not None:
                protection_payload.update(
                    {
                        "required_pull_request_reviews": protection.get(
                            "required_pull_request_reviews"
                        ),
                        "enforce_admins": protection.get("enforce_admins"),
                    }
                )
            yield event(
                "repo.branch_protection.snapshot",
                f"{node_id}:protection@{snapshot_ts}",
                snapshot_ts,
                protection_payload,
            )

            security = self._get(f"/repos/{full}/contents/SECURITY.md")
            if security is not None and security.get("content"):
                content = base64.b64decode(security["content"]).decode("utf-8", errors="replace")
                yield event(
                    "repo.security_policy.snapshot",
                    f"{node_id}:security@{snapshot_ts}",
                    snapshot_ts,
                    {
                        "org": org,
                        "repo": repo["name"],
                        "repo_node_id": node_id,
                        "path": "SECURITY.md",
                        "sha": security.get("sha"),
                        "updated_at": snapshot_ts,
                        "content": content,
                    },
                )

            listing = self._get(f"/repos/{full}/contents")
            if listing:
                files: list[dict[str, Any]] = []
                for item in listing:
                    if item.get("type") != "file":
                        continue
                    entry: dict[str, Any] = {
                        "path": item["path"],
                        "sha": item["sha"],
                        "size": item["size"],
                    }
                    if item["size"] <= _MAX_INLINE_FILE_BYTES:
                        blob = self._get(f"/repos/{full}/contents/{item['path']}")
                        if blob and blob.get("content"):
                            entry["content"] = base64.b64decode(blob["content"]).decode(
                                "utf-8", errors="replace"
                            )
                    files.append(entry)
                yield event(
                    "repo.file_listing.snapshot",
                    f"{node_id}:files@{snapshot_ts}",
                    snapshot_ts,
                    {
                        "org": org,
                        "repo": repo["name"],
                        "repo_node_id": node_id,
                        "updated_at": snapshot_ts,
                        "files": files,
                    },
                )
