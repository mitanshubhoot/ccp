"""The live GitHub client's conformance suite (spec.md A-122).

The recordings are written from GitHub's documented responses, not captured from a live
organization: one organization, members (the first is an admin), repositories with direct
collaborators, branch protection and SECURITY.md on the first repository, one small file in
each. A scenario changes what one path answers. The safety check reads every recording, so a
capture added later cannot carry a token in.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from ccp.connectors.base import Connector, ScopeStatus, SourceEvent, SyncReport
from ccp.connectors.conformance import Control, Expect, Scenario, Sleep
from ccp.connectors.github.client import NOT_VISIBLE, GitHubLiveConnector
from ccp.connectors.rate import RateBucket
from ccp.tenancy import TenantContext

ORG = "conformance-example"
PAGE = 100
Respond = Callable[[httpx.Request], httpx.Response | None]


def _ok(body: Any) -> httpx.Response:
    return httpx.Response(200, json=body)


def _said(status: int, message: str, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"message": message}, headers=headers)


def once(*responses: httpx.Response) -> Respond:
    """Answer with each response in turn, then as recorded."""
    queue = list(responses)
    return lambda request: queue.pop(0) if queue else None


def always(response: httpx.Response) -> Respond:
    return lambda request: response


def on_page(page: int, response: httpx.Response) -> Respond:
    return lambda request: response if request.url.params.get("page") == str(page) else None


def raising(make: Callable[[httpx.Request], Exception]) -> Respond:
    def respond(request: httpx.Request) -> httpx.Response | None:
        raise make(request)

    return respond


@dataclass
class Recording:
    """The recorded API: a world, and what a scenario changes on given paths."""

    members: int = 2
    repos: int = 2
    collaborators: int = 1
    changes: dict[str, Respond] = field(default_factory=dict)

    def _logins(self) -> list[str]:
        return [f"member-{i}" for i in range(1, self.members + 1)]

    def _repo_names(self) -> list[str]:
        return [f"repo-{j}" for j in range(1, self.repos + 1)]

    @staticmethod
    def _page(items: list[Any], request: httpx.Request) -> list[Any]:
        page = int(request.url.params.get("page", "1"))
        return items[(page - 1) * PAGE : page * PAGE]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        change = self.changes.get(path)
        if change is not None:
            answer = change(request)
            if answer is not None:
                return answer
        return self._recorded(request, path)

    def _recorded(self, request: httpx.Request, path: str) -> httpx.Response:
        logins = self._logins()
        user = [{"login": x, "node_id": f"U_{x}"} for x in logins]
        if path == f"/orgs/{ORG}":
            return _ok(
                {"node_id": "O_conformance", "login": ORG, "updated_at": "2026-09-01T00:00:00Z"}
            )
        if path == f"/orgs/{ORG}/members":
            admins = user[:1] if request.url.params.get("role") == "admin" else user
            return _ok(self._page(admins, request))
        if path == "/graphql":
            login = json.loads(request.content)["variables"]["login"]
            emails = [f"{login}@conformance.example"]
            return _ok({"data": {"user": {"organizationVerifiedDomainEmails": emails}}})
        if path == f"/orgs/{ORG}/repos":
            repos = [
                {
                    "full_name": f"{ORG}/{name}",
                    "name": name,
                    "node_id": f"R_{name}",
                    "default_branch": "main",
                    "private": True,
                    "updated_at": "2026-09-01T00:00:00Z",
                }
                for name in self._repo_names()
            ]
            return _ok(self._page(repos, request))
        for name in self._repo_names():
            base = f"/repos/{ORG}/{name}"
            if path == f"{base}/collaborators":
                listed = [dict(u, role_name="write") for u in user[: self.collaborators]]
                return _ok(self._page(listed, request))
            if path == f"{base}/branches/main/protection":
                if name != "repo-1":
                    return _said(404, "Branch not protected")
                return _ok(
                    {
                        "required_pull_request_reviews": {"required_approving_review_count": 1},
                        "enforce_admins": {"enabled": True},
                    }
                )
            if path == f"{base}/contents/SECURITY.md":
                if name != "repo-1":
                    return _said(404, "Not Found")
                body = base64.b64encode(b"Report issues to security@conformance.example").decode()
                return _ok({"content": body, "sha": "sec1"})
            if path == f"{base}/contents":
                return _ok([{"type": "file", "path": "README.md", "sha": f"r-{name}", "size": 9}])
            if path == f"{base}/contents/README.md":
                return _ok({"content": base64.b64encode(b"# readme\n").decode(), "sha": "r"})
        return _said(404, "Not Found")

    def dump(self) -> str:
        """What this recording answers, bodies and headers, for the safety check."""
        tails = ("collaborators", "branches/main/protection", "contents/SECURITY.md", "contents")
        seen = [f"/orgs/{ORG}", f"/orgs/{ORG}/members", f"/orgs/{ORG}/repos"]
        seen += [f"/repos/{ORG}/{n}/{tail}" for n in self._repo_names() for tail in tails]
        out = []
        for path in seen:
            response = self._recorded(httpx.Request("GET", f"https://api.github.com{path}"), path)
            out.append(f"{path} {response.status_code} {response.text}")
            out.append(json.dumps(dict(response.headers)))
        return "\n".join(out)


def _marker(events: list[SourceEvent]) -> dict[str, Any] | None:
    found = [e for e in events if e.event_type == "org.pull.completed"]
    return found[-1].payload if found else None


def _all_members_seen(count: int) -> Callable[[list[SourceEvent]], list[str]]:
    def check(events: list[SourceEvent]) -> list[str]:
        marker = _marker(events) or {}
        problems = []
        members = len(marker.get("members") or [])
        if members != count:
            problems.append(f"{count} members across two pages; the pull saw {members}")
        listed = len((marker.get("repo_collaborators") or {}).get("R_repo-1") or [])
        if listed != count:
            problems.append(f"{count} collaborators across two pages; the pull saw {listed}")
        return problems

    return check


SCOPES = ("org", "members", "repos", "collaborators", "repo_settings")
ALL_COMPLETE: dict[str, ScopeStatus] = dict.fromkeys(SCOPES, "complete")


def scenarios() -> list[tuple[Scenario, Callable[[], Recording]]]:
    members = f"/orgs/{ORG}/members"
    plan = "Upgrade to GitHub Pro or make this repository public to enable this feature."
    return [
        (
            Scenario(
                "normal",
                "two members, two repositories",
                Expect("complete", None, ALL_COMPLETE),
                repeatable=True,
            ),
            Recording,
        ),
        (
            Scenario(
                "empty-organization",
                "an owner and no repositories: empty listings are facts",
                Expect("complete", None, ALL_COMPLETE),
                repeatable=True,
            ),
            lambda: Recording(members=1, repos=0),
        ),
        (
            Scenario(
                "page-boundary",
                "101 members and 101 collaborators: a full first page, then one more",
                Expect("complete", None, ALL_COMPLETE),
                check=_all_members_seen(101),
            ),
            lambda: Recording(members=101, repos=1, collaborators=101),
        ),
        (
            Scenario("credential-refused", "401 on the member listing", Expect(stops="auth")),
            lambda: Recording(changes={members: always(_said(401, "Bad credentials"))}),
        ),
        (
            Scenario(
                "sso-not-authorized",
                "403 naming SAML single sign-on",
                Expect(stops="auth"),
            ),
            lambda: Recording(
                changes={
                    f"/orgs/{ORG}": always(
                        _said(403, "Resource protected by organization SAML enforcement.")
                    )
                }
            ),
        ),
        (
            Scenario(
                "rate-limited-then-served",
                "403 with no quota left and Retry-After 7, then the listing",
                Expect("complete", None, ALL_COMPLETE, waits=(7.0,)),
            ),
            lambda: Recording(
                changes={
                    members: once(
                        _said(
                            403,
                            "API rate limit exceeded",
                            **{"x-ratelimit-remaining": "0", "retry-after": "7"},
                        )
                    )
                }
            ),
        ),
        (
            Scenario(
                "too-many-requests",
                "429 with Retry-After 3 on the repository listing, then the listing",
                Expect("complete", None, ALL_COMPLETE, waits=(3.0,)),
            ),
            lambda: Recording(
                changes={
                    f"/orgs/{ORG}/repos": once(_said(429, "slow down", **{"retry-after": "3"}))
                }
            ),
        ),
        (
            Scenario(
                "member-listing-hidden",
                "404 on the member listing: not visible, never empty",
                Expect("partial", None, {"members": "not_visible", "repos": "complete"}),
                hidden=frozenset({"members"}),
            ),
            lambda: Recording(changes={members: always(_said(404, "Not Found"))}),
        ),
        (
            Scenario(
                "collaborators-hidden-on-one-repository",
                "403 on one repository's collaborators: push access needed",
                Expect("partial", None, {"collaborators": "partial", "members": "complete"}),
                hidden=frozenset({"R_repo-2"}),
            ),
            lambda: Recording(
                changes={
                    f"/repos/{ORG}/repo-2/collaborators": always(
                        _said(403, "Must have push access to view repository collaborators.")
                    )
                }
            ),
        ),
        (
            Scenario(
                "server-error-mid-listing",
                "page two of the member listing answers 500 every time",
                Expect(stops="fail"),
                hidden=frozenset({"members"}),
            ),
            lambda: Recording(members=150, changes={members: on_page(2, httpx.Response(500))}),
        ),
        (
            Scenario(
                "timeout",
                "the collaborators listing never answers",
                Expect(stops="fail"),
            ),
            lambda: Recording(
                changes={
                    f"/repos/{ORG}/repo-1/collaborators": raising(
                        lambda request: httpx.ReadTimeout("timed out", request=request)
                    )
                }
            ),
        ),
        (
            Scenario(
                "unprotected-plan-and-empty-repository",
                "403 on branch protection (plan), 409 on an empty repository's contents",
                Expect("partial", None, {"repo_settings": "partial", "collaborators": "complete"}),
            ),
            lambda: Recording(
                changes={
                    f"/repos/{ORG}/repo-1/branches/main/protection": always(_said(403, plan)),
                    f"/repos/{ORG}/repo-2/contents": always(_said(409, "Git Repository is empty.")),
                }
            ),
        ),
        (
            Scenario(
                "organization-out-of-sight",
                "404 on the organization itself",
                Expect(stops="not_visible"),
            ),
            lambda: Recording(changes={f"/orgs/{ORG}": always(_said(404, "Not Found"))}),
        ),
    ]


def _connector(cls: type[GitHubLiveConnector], recording: Recording, sleep: Sleep) -> Connector:
    return cls(
        "conformance",  # not a credential: the recordings answer whatever it says
        transport=httpx.MockTransport(recording.handler),
        sleep=sleep,
        clock=lambda: datetime(2026, 11, 1, tzinfo=UTC),
        bucket=RateBucket(rate=1_000_000.0, burst=1_000_000),
        org=ORG,
    )


class Suite:
    connector = "github"

    def __init__(self, cls: type[GitHubLiveConnector] = GitHubLiveConnector) -> None:
        self._cls = cls
        self._recordings = {s.name: make for s, make in scenarios()}
        self.scenarios: tuple[Scenario, ...] = tuple(s for s, _ in scenarios())

    def build(self, scenario: Scenario, sleep: Sleep) -> Connector:
        return _connector(self._cls, self._recordings[scenario.name](), sleep)

    def settled(self, events: list[SourceEvent]) -> set[str] | None:
        marker = _marker(events)
        if marker is None:
            return set()  # a pull that stopped settles nothing
        listed = set((marker.get("repo_collaborators") or {}).keys())
        if marker.get("members_listed", marker.get("complete")):
            listed.add("members")
        return listed

    def recordings(self) -> Iterable[str]:
        return [make().dump() for make in self._recordings.values()]

    def close(self) -> None:
        return None


# -- negative controls: the real client with one promise broken -------------------------


class _HiddenReadAsEmpty(GitHubLiveConnector):
    """Reads a listing it cannot see as empty, and forgets the gap: the defect that would
    revoke every member the moment a token lost access."""

    def _get(self, path: str, endpoint: str, **params: Any) -> Any:
        got = super()._get(path, endpoint, **params)
        if got is NOT_VISIBLE:
            self.incomplete.pop()
            return []
        return got


class _CompleteOverGaps(GitHubLiveConnector):
    """Claims every scope complete, whatever it saw."""

    def pull(
        self, tenant: TenantContext, since: datetime | None = None, report: SyncReport | None = None
    ) -> Iterator[SourceEvent]:
        def events() -> Iterator[SourceEvent]:
            yield from super(_CompleteOverGaps, self).pull(tenant, since, report)
            if report is not None:
                for scope in self.scopes:
                    report.complete(scope, 0)

        return events()


class _IdsFromThePullTime(GitHubLiveConnector):
    """Snapshot ids that carry the pull, so an unchanged re-pull lands everything again."""

    def pull(
        self, tenant: TenantContext, since: datetime | None = None, report: SyncReport | None = None
    ) -> Iterator[SourceEvent]:
        stamp = uuid.uuid4().hex[:8]
        return (
            e.model_copy(update={"source_event_id": f"{e.source_event_id}@{stamp}"})
            for e in super().pull(tenant, since, report)
        )


def controls() -> list[Control]:
    return [
        Control(
            "github: hidden listing read as empty",
            "absence only from a whole listing",
            Suite(_HiddenReadAsEmpty),
        ),
        Control(
            "github: complete over a gap",
            "a hidden scope is never complete",
            Suite(_CompleteOverGaps),
        ),
        Control(
            "github: ids from the pull time",
            "an unchanged re-pull is the same",
            Suite(_IdsFromThePullTime),
        ),
    ]
