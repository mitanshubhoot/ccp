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

Reliability (spec.md A-102): every request retries with backoff on 5xx and transport
errors and waits out a rate limit (429, or 403 with no remaining quota) before trying
again; any other error stops the pull. A 404 on a listing means "not visible", never
"empty": the pull is marked incomplete. A failed email lookup raises. Snapshot ids are
derived from content, so an unchanged re-pull lands nothing. The last event of every pull,
`org.pull.completed`, says whether the pull saw everything; only a complete pull lets the
normalizer read a missing member or collaborator as a revocation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from ccp.connectors.base import ConnectorName, SourceEvent, SourceKind, SyncReport, after
from ccp.tenancy import TenantContext

SCHEMA_VERSION = "github.v1"
_MAX_INLINE_FILE_BYTES = 20_000
_VERIFIED_EMAILS_QUERY = """
query($login: String!, $org: String!) {
  user(login: $login) { organizationVerifiedDomainEmails(login: $org) }
}
"""


RETRY_STATUSES = frozenset({500, 502, 503, 504})
MAX_ATTEMPTS = 5
MAX_WAIT_SECONDS = 120.0


def _rate_limit_wait(response: httpx.Response) -> float | None:
    """Seconds to wait when the response is a rate limit, else None. GitHub signals one with
    429, or with 403 and no remaining quota; Retry-After or the reset time says how long."""
    limited = response.status_code == 429 or (
        response.status_code == 403
        and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in response.headers
        )
    )
    if not limited:
        return None
    if "retry-after" in response.headers:
        return max(1.0, float(response.headers["retry-after"]))
    reset = response.headers.get("x-ratelimit-reset")
    if reset is not None:
        return max(1.0, float(reset) - time.time())
    return 60.0


class GitHubPullError(RuntimeError):
    """The pull cannot be trusted: an error that retries did not clear."""


def content_id(prefix: str, payload: dict[str, Any]) -> str:
    """An event id from what was seen, not when: the pull time is left out, so the same
    content pulled again is the same event and lands nothing (A-102)."""
    material = {k: v for k, v in payload.items() if k not in ("updated_at", "pulled_at")}
    canonical = json.dumps(material, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}@{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"


class GitHubLiveConnector:
    name: ConnectorName = "github"
    source: SourceKind = "live"
    scopes: tuple[str, ...] = ("org", "members", "repos", "collaborators", "repo_settings")

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
            transport=transport,
        )
        self._sleep = sleep
        self.incomplete: list[str] = []  # listings this pull could not see in full

    def pull(
        self,
        tenant: TenantContext,
        since: datetime | None = None,
        report: SyncReport | None = None,
    ) -> Iterator[SourceEvent]:
        return after(self._events(tenant, report), since)

    def _gaps(self, fragment: str) -> list[str]:
        return [g for g in self.incomplete if fragment in g]

    # -- helpers -------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One request with retries: 5xx and transport errors back off exponentially with
        jitter; a rate limit waits for its reset (capped); anything else returns."""
        last = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wait = min(MAX_WAIT_SECONDS, 2.0 ** (attempt - 1)) * random.uniform(1.0, 1.5)  # noqa: S311
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                limited = _rate_limit_wait(response)
                if limited is not None:
                    last, wait = f"rate limited ({response.status_code})", limited
                elif response.status_code in RETRY_STATUSES:
                    last = f"HTTP {response.status_code}"
                else:
                    return response
            if attempt < MAX_ATTEMPTS:
                self._sleep(min(wait, MAX_WAIT_SECONDS))
        raise GitHubPullError(f"{method} {path}: gave up after {MAX_ATTEMPTS} attempts ({last})")

    def _get(self, path: str, *, optional: bool = False, **params: Any) -> Any:
        """A 404 on an optional resource (branch protection, SECURITY.md, a file) is "not
        there"; on a listing it is "not visible to this token", so the pull is incomplete."""
        response = self._request("GET", path, params=params or None)
        if response.status_code == 404:
            if not optional:
                self.incomplete.append(f"404 {path}")
            return None
        if response.status_code >= 400:
            raise GitHubPullError(f"GET {path}: HTTP {response.status_code}")
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
        """A failed lookup raises (A-102): reading it as "no emails" would make an account
        look unlinkable to its person."""
        response = self._request(
            "POST",
            "/graphql",
            json={"query": _VERIFIED_EMAILS_QUERY, "variables": {"login": login, "org": org}},
        )
        body = response.json() if response.status_code == 200 else {}
        if response.status_code != 200 or body.get("errors"):
            detail = body.get("errors") or f"HTTP {response.status_code}"
            raise GitHubPullError(f"verified-email lookup for {login!r} failed: {detail}")
        data = body.get("data") or {}
        user = data.get("user") or {}
        emails = user.get("organizationVerifiedDomainEmails") or []
        return [e for e in emails if isinstance(e, str)]

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    # -- the pull ------------------------------------------------------------------

    def _events(
        self, tenant: TenantContext, report: SyncReport | None = None
    ) -> Iterator[SourceEvent]:
        org = tenant.settings.github_org
        snapshot_ts = self._now()
        self.incomplete = []  # this pull's gaps only, whatever an earlier pull saw

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
        yield event("org.snapshot", content_id(org_obj["node_id"], org_obj), org_updated, org_obj)
        if report is not None:
            report.complete("org", 1)

        admins = {m["login"] for m in self._paginate(f"/orgs/{org}/members", role="admin")}
        members = self._paginate(f"/orgs/{org}/members", role="all")
        if report is not None:
            hidden = self._gaps(f"/orgs/{org}/members")
            if hidden:
                report.mark("members", "not_visible", 0, "; ".join(hidden))
            else:
                report.complete("members", len(members))
        seen_collaborators: dict[str, list[str]] = {}
        for member in members:
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
                "org.member.snapshot", content_id(member["node_id"], payload), snapshot_ts, payload
            )

        repos = self._paginate(f"/orgs/{org}/repos", type="all")
        repos_hidden = self._gaps(f"/orgs/{org}/repos")
        if report is not None:
            if repos_hidden:
                report.mark("repos", "not_visible", 0, "; ".join(repos_hidden))
            else:
                report.complete("repos", len(repos))
        for repo in repos:
            full = repo["full_name"]
            node_id = repo["node_id"]
            updated = repo.get("updated_at") or snapshot_ts
            gaps_before = len(self.incomplete)
            collaborators = [
                {
                    "login": c["login"],
                    "node_id": c["node_id"],
                    "type": c.get("type", "User"),
                    "permission": c.get("role_name") or c.get("permissions", {}),
                }
                for c in self._paginate(f"/repos/{full}/collaborators", affiliation="direct")
            ]
            if len(self.incomplete) == gaps_before:  # this repo's collaborators seen in full
                seen_collaborators[node_id] = sorted(c["node_id"] for c in collaborators)
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
            yield event("repo.snapshot", content_id(node_id, repo_payload), updated, repo_payload)

            protection = self._get(
                f"/repos/{full}/branches/{repo_payload['default_branch']}/protection",
                optional=True,
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
                content_id(f"{node_id}:protection", protection_payload),
                snapshot_ts,
                protection_payload,
            )

            security = self._get(f"/repos/{full}/contents/SECURITY.md", optional=True)
            if security is not None and security.get("content"):
                content = base64.b64decode(security["content"]).decode("utf-8", errors="replace")
                policy = {
                    "org": org,
                    "repo": repo["name"],
                    "repo_node_id": node_id,
                    "path": "SECURITY.md",
                    "sha": security.get("sha"),
                    "updated_at": snapshot_ts,
                    "content": content,
                }
                yield event(
                    "repo.security_policy.snapshot",
                    content_id(f"{node_id}:security", policy),
                    snapshot_ts,
                    policy,
                )

            listing = self._get(f"/repos/{full}/contents", optional=True)  # empty repo: 404
            if listing:
                files: list[dict[str, Any]] = []
                not_listed: list[dict[str, Any]] = []  # GH-5 cannot pass over these (A-96)
                for item in listing:
                    if item.get("type") != "file":
                        not_listed.append({"path": item.get("path"), "type": item.get("type")})
                        continue
                    entry: dict[str, Any] = {
                        "path": item["path"],
                        "sha": item["sha"],
                        "size": item["size"],
                    }
                    if item["size"] <= _MAX_INLINE_FILE_BYTES:
                        blob = self._get(f"/repos/{full}/contents/{item['path']}", optional=True)
                        if blob and blob.get("content"):
                            entry["content"] = base64.b64decode(blob["content"]).decode(
                                "utf-8", errors="replace"
                            )
                    files.append(entry)
                listing_payload = {
                    "org": org,
                    "repo": repo["name"],
                    "repo_node_id": node_id,
                    "updated_at": snapshot_ts,
                    "files": files,
                    "not_listed": not_listed,
                }
                yield event(
                    "repo.file_listing.snapshot",
                    content_id(f"{node_id}:files", listing_payload),
                    snapshot_ts,
                    listing_payload,
                )

        if report is not None:
            listed = len(seen_collaborators)
            if repos_hidden:
                report.mark("collaborators", "not_visible", 0, "the repositories are not visible")
            elif listed == len(repos):
                report.complete("collaborators", listed)
            elif listed:
                report.mark(
                    "collaborators",
                    "partial",
                    listed,
                    f"{len(repos) - listed} of {len(repos)} repositories' collaborators not"
                    " visible",
                )
            else:
                report.mark("collaborators", "not_visible", 0, "; ".join(self._gaps("/repos/")))
            report.complete("repo_settings", len(repos))
        completed = {
            "org": org,
            "complete": not self.incomplete,
            "incomplete": list(self.incomplete),
            "members": sorted(m["node_id"] for m in members),
            "repo_collaborators": seen_collaborators,
            "pulled_at": snapshot_ts,
        }
        yield event(
            "org.pull.completed", content_id(f"{org}:pull", completed), snapshot_ts, completed
        )
