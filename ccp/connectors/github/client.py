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

Reliability (spec.md A-102, A-120): what a response means comes from the per-endpoint
error table (`errors.py`), never from its status alone. A transient error retries with
backoff and a rate limit waits for its reset; every request first spends a token from the
tenant's bucket for the organization (`ccp.connectors.rate`). A read that is absent is a
fact (an unprotected branch, an empty repository); one that is not visible is a gap that
leaves its scope partial while the pull goes on; a refused credential stops the pull as an
auth failure, and anything else unexpected stops it too. A failed email lookup raises.
Snapshot ids are derived from content, so an unchanged re-pull lands no snapshot. The last
event of every pull, `org.pull.completed`, is the pull's own (its id is new each time): it
names the listings the pull saw whole and who was on them, and from it the normalizer
settles every grant on those listings at the pull time (A-119): not listed is revoked,
listed is still active, and listed again after a revocation is active again.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import json
import random
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from ccp.connectors import rate
from ccp.connectors.base import (
    ConnectorName,
    PullError,
    PullErrorKind,
    SourceEvent,
    SourceKind,
    SyncReport,
    after,
)
from ccp.connectors.github import errors
from ccp.tenancy import TenantContext

SCHEMA_VERSION = "github.v1"
_MAX_INLINE_FILE_BYTES = 20_000
_VERIFIED_EMAILS_QUERY = """
query($login: String!, $org: String!) {
  user(login: $login) { organizationVerifiedDomainEmails(login: $org) }
}
"""


MAX_ATTEMPTS = 5
MAX_WAIT_SECONDS = 120.0
# a connection's bucket: steady, well under GitHub's secondary limit of 900 REST points a
# minute, and a reserve of the hourly quota left for the customer's other tools (A-120)
RATE_PER_SECOND = 10.0
BURST = 50
QUOTA_RESERVE = 100


def _rate_limit_wait(response: httpx.Response) -> float:
    """How long a rate limit asks to wait: Retry-After, else until the reset, else a minute."""
    if "retry-after" in response.headers:
        return max(1.0, float(response.headers["retry-after"]))
    reset = response.headers.get("x-ratelimit-reset")
    if reset is not None:
        return max(1.0, float(reset) - time.time())
    return 60.0


class GitHubPullError(PullError):
    """The pull cannot be trusted: an error that retries did not clear, or a response the
    error table says stops the pull. Its kind is auth, not_visible or fail (A-120)."""


class Gap(enum.Enum):
    """A read with no body, by the error table: absent is a fact, not visible is a gap."""

    ABSENT = "absent"
    NOT_VISIBLE = "not_visible"


ABSENT = Gap.ABSENT
NOT_VISIBLE = Gap.NOT_VISIBLE


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
        clock: Callable[[], datetime] | None = None,
        bucket: rate.RateBucket | None = None,
        org: str | None = None,
        connection_id: str | None = None,
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
        self._clock = clock or (lambda: datetime.now(UTC))
        self._given_bucket = bucket
        self._bucket: rate.RateBucket | None = bucket  # else the tenant's, chosen per pull
        self._org = org  # the connection's account; else the tenant's organization
        self._connection_id = connection_id
        self.incomplete: list[str] = []  # what this pull could not see: path and reason

    def pull(
        self,
        tenant: TenantContext,
        since: datetime | None = None,
        report: SyncReport | None = None,
    ) -> Iterator[SourceEvent]:
        return after(self._events(tenant, report), since)

    def _gaps(self, fragment: str) -> list[str]:
        return [g for g in self.incomplete if fragment in g]

    def _start(self, tenant: TenantContext) -> str:
        """Ready a pull or a check: the organization, this pull's gaps cleared, and the bucket
        it spends from, one per (tenant, connection) (A-120, A-121)."""
        org = self._org or tenant.settings.github_org
        self.incomplete = []  # this pull's gaps only, whatever an earlier pull saw
        self._bucket = self._given_bucket or rate.bucket_for(
            tenant.tenant_id,
            self._connection_id or f"github:{org}",
            lambda: rate.RateBucket(RATE_PER_SECOND, BURST, QUOTA_RESERVE),
        )
        return org

    def check(self, tenant: TenantContext) -> tuple[str, str]:
        """The daily probe of the credential (A-121): can it see the organization and its
        members? Two cheap reads, through the same error table and bucket as a pull."""
        org = self._start(tenant)
        probes = ((f"/orgs/{org}", errors.ORG, None), (f"/orgs/{org}/members", errors.MEMBERS, 1))
        for path, endpoint, per_page in probes:
            params = {"per_page": per_page} if per_page else None
            response, meaning, why = self._request("GET", path, endpoint, params=params)
            if meaning != "ok":
                kind = meaning if meaning in ("auth", "not_visible") else "fail"
                return kind, f"GET {path}: HTTP {response.status_code} ({why})"
        return "ok", f"the organization {org} and its members are visible"

    # -- helpers -------------------------------------------------------------------

    def _request(
        self, method: str, path: str, endpoint: str, **kwargs: Any
    ) -> tuple[httpx.Response, errors.Meaning, str]:
        """One request with retries, by what the error table says the response means: retry
        backs off exponentially with jitter, rate_limited waits for the reset (capped), and
        any other meaning returns. Every attempt first spends from the connection's bucket."""
        last = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wait = min(MAX_WAIT_SECONDS, 2.0 ** (attempt - 1)) * random.uniform(1.0, 1.5)  # noqa: S311
            if self._bucket is not None:
                self._bucket.take(self._sleep)
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                self._observe_quota(response)
                meaning, why = errors.classify(endpoint, response)
                if meaning == "rate_limited":
                    last, wait = why, _rate_limit_wait(response)
                elif meaning == "retry":
                    last = why
                else:
                    return response, meaning, why
            if attempt < MAX_ATTEMPTS:
                self._sleep(min(wait, MAX_WAIT_SECONDS))
        raise GitHubPullError(f"{method} {path}: gave up after {MAX_ATTEMPTS} attempts ({last})")

    def _observe_quota(self, response: httpx.Response) -> None:
        """GitHub's own count of the hourly quota left holds the bucket near the end."""
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if self._bucket is None or remaining is None or reset is None:
            return
        try:
            self._bucket.observe(int(remaining), float(reset) - time.time())
        except ValueError:
            return

    def _get(self, path: str, endpoint: str, **params: Any) -> Any:
        """The body when the read is ok; ABSENT, or NOT_VISIBLE with its gap recorded, by the
        error table; a refused credential or anything unexpected raises (A-120)."""
        response, meaning, why = self._request("GET", path, endpoint, params=params or None)
        if meaning == "ok":
            return response.json()
        if meaning == "absent":
            return ABSENT
        if meaning == "not_visible":
            self.incomplete.append(f"{response.status_code} {path}: {why}")
            return NOT_VISIBLE
        kind: PullErrorKind = "auth" if meaning == "auth" else "fail"
        raise GitHubPullError(f"GET {path}: HTTP {response.status_code} ({why})", kind)

    def _paginate(self, path: str, endpoint: str, **params: Any) -> list[dict[str, Any]]:
        """Every page of a listing; a page not visible ends it with its gap recorded, so the
        caller can tell the listing is not whole."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._get(path, endpoint, per_page=100, page=page, **params)
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    def _verified_emails(self, login: str, org: str) -> list[str]:
        """A failed lookup raises (A-102): reading it as "no emails" would make an account
        look unlinkable to its person."""
        response, meaning, why = self._request(
            "POST",
            "/graphql",
            errors.GRAPHQL,
            json={"query": _VERIFIED_EMAILS_QUERY, "variables": {"login": login, "org": org}},
        )
        body = response.json() if meaning == "ok" else {}
        if meaning != "ok" or body.get("errors"):
            detail = body.get("errors") or f"HTTP {response.status_code} ({why})"
            kind: PullErrorKind = "auth" if meaning == "auth" else "fail"
            raise GitHubPullError(f"verified-email lookup for {login!r} failed: {detail}", kind)
        data = body.get("data") or {}
        user = data.get("user") or {}
        emails = user.get("organizationVerifiedDomainEmails") or []
        return [e for e in emails if isinstance(e, str)]

    def _now(self) -> str:
        """The pull time, to the microsecond: two pulls are never the same instant, so the
        later one's settlement of a grant always reads as the later observation (A-119)."""
        return self._clock().astimezone(UTC).isoformat().replace("+00:00", "Z")

    # -- the pull ------------------------------------------------------------------

    def _events(
        self, tenant: TenantContext, report: SyncReport | None = None
    ) -> Iterator[SourceEvent]:
        org = self._start(tenant)
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

        org_obj = self._get(f"/orgs/{org}", errors.ORG)
        if not isinstance(org_obj, dict):
            raise GitHubPullError(
                f"GitHub organization {org!r} does not exist or this credential cannot see it",
                "not_visible",
            )
        org_updated = org_obj.get("updated_at") or snapshot_ts
        yield event("org.snapshot", content_id(org_obj["node_id"], org_obj), org_updated, org_obj)
        if report is not None:
            report.complete("org", 1)

        admins = {
            m["login"] for m in self._paginate(f"/orgs/{org}/members", errors.MEMBERS, role="admin")
        }
        members = self._paginate(f"/orgs/{org}/members", errors.MEMBERS, role="all")
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

        repos = self._paginate(f"/orgs/{org}/repos", errors.REPOS, type="all")
        repos_hidden = self._gaps(f"/orgs/{org}/repos")
        if report is not None:
            if repos_hidden:
                report.mark("repos", "not_visible", 0, "; ".join(repos_hidden))
            else:
                report.complete("repos", len(repos))
        settings_hidden: list[str] = []  # repositories with a setting this pull could not read
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
                for c in self._paginate(
                    f"/repos/{full}/collaborators", errors.COLLABORATORS, affiliation="direct"
                )
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

            hidden_here: list[str] = []
            protection = self._get(
                f"/repos/{full}/branches/{repo_payload['default_branch']}/protection",
                errors.PROTECTION,
            )
            if protection is NOT_VISIBLE:  # no fact either way: GH-3 abstains
                hidden_here.append("branch protection")
            else:
                protection_payload: dict[str, Any] = {
                    "org": org,
                    "repo": repo["name"],
                    "repo_node_id": node_id,
                    "branch": repo_payload["default_branch"],
                    "protected": isinstance(protection, dict),
                    "updated_at": snapshot_ts,
                }
                if isinstance(protection, dict):
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

            security = self._get(f"/repos/{full}/contents/SECURITY.md", errors.FILE)
            if security is NOT_VISIBLE:
                hidden_here.append("SECURITY.md")
            elif isinstance(security, dict) and security.get("content"):
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

            listing = self._get(f"/repos/{full}/contents", errors.CONTENTS)
            if listing is NOT_VISIBLE:
                hidden_here.append("files")
            else:
                files: list[dict[str, Any]] = []
                not_listed: list[dict[str, Any]] = []  # GH-5 cannot pass over these (A-96)
                for item in listing if isinstance(listing, list) else []:  # absent: no files
                    if item.get("type") != "file":
                        not_listed.append({"path": item.get("path"), "type": item.get("type")})
                        continue
                    entry: dict[str, Any] = {
                        "path": item["path"],
                        "sha": item["sha"],
                        "size": item["size"],
                    }
                    if item["size"] <= _MAX_INLINE_FILE_BYTES:
                        blob = self._get(f"/repos/{full}/contents/{item['path']}", errors.FILE)
                        if isinstance(blob, dict) and blob.get("content"):
                            entry["content"] = base64.b64decode(blob["content"]).decode(
                                "utf-8", errors="replace"
                            )
                        elif blob is NOT_VISIBLE:  # not inlined, so GH-5 cannot pass over it
                            hidden_here.append(f"file {item['path']}")
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
            if hidden_here:
                settings_hidden.append(f"{full}: {', '.join(hidden_here)} not visible")

        if report is not None:
            listed = len(seen_collaborators)
            if repos_hidden:
                report.mark("collaborators", "not_visible", 0, "the repositories are not visible")
                report.mark("repo_settings", "not_visible", 0, "the repositories are not visible")
            else:
                if listed == len(repos):
                    report.complete("collaborators", listed)
                elif listed:
                    report.mark(
                        "collaborators",
                        "partial",
                        listed,
                        f"{len(repos) - listed} of {len(repos)} repositories' collaborators"
                        " not visible",
                    )
                else:
                    gaps = "; ".join(self._gaps("/collaborators"))
                    report.mark("collaborators", "not_visible", 0, gaps)
                if settings_hidden:
                    report.mark(
                        "repo_settings",
                        "partial",
                        len(repos) - len(settings_hidden),
                        "; ".join(settings_hidden),
                    )
                else:
                    report.complete("repo_settings", len(repos))
        completed = {
            "org": org,
            "complete": not self.incomplete,
            "incomplete": list(self.incomplete),
            "members_listed": not self._gaps(f"/orgs/{org}/members"),
            "members": sorted(m["node_id"] for m in members),
            "repo_collaborators": seen_collaborators,
            "pulled_at": snapshot_ts,
        }
        # the pull's own event, new every pull unlike the snapshots: the record that these
        # listings were seen whole at this time, from which every grant on them is settled
        yield event(
            "org.pull.completed", f"{org}:pull@{uuid.uuid4().hex[:16]}", snapshot_ts, completed
        )
