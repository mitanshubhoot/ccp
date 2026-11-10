"""MT1 (A-120): the live GitHub client against a recorded API, by the error table.

The defects: every client error but a 404 stopped the whole pull, so one private repository
on a plan without branch protection (403), one repository whose collaborators need push
access to list (403), or one empty repository (409 on its contents) ended the pull for the
whole organization, and a refused credential looked like any other failure.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ccp.config import Settings
from ccp.connectors.base import SourceEvent, SyncReport
from ccp.connectors.github.client import GitHubLiveConnector, GitHubPullError
from ccp.connectors.rate import RateBucket
from ccp.tenancy import TenantContext
from tests.component.test_github_recorded import ORG, REPO, _api

PROTECTION = f"/repos/{REPO}/branches/main/protection"
CONTENTS = f"/repos/{REPO}/contents"
COLLABORATORS = f"/repos/{REPO}/collaborators"
MEMBERS = f"/orgs/{ORG}/members"


def _pull(
    overrides: dict[str, list[httpx.Response]], **kwargs: Any
) -> tuple[list[SourceEvent], SyncReport]:
    handler, _ = _api(["ana"], overrides=overrides)
    connector = GitHubLiveConnector(
        "t", transport=httpx.MockTransport(handler), sleep=lambda s: None, **kwargs
    )
    report = SyncReport(connector.scopes)
    tenant = TenantContext.for_id(Settings(), "acme")
    return list(connector.pull(tenant, None, report)), report


def _message(status: int, message: str) -> list[httpx.Response]:
    return [httpx.Response(status, json={"message": message})]


def test_a_plan_without_protection_and_an_empty_repository_leave_the_pull_going() -> None:
    events, report = _pull(
        {
            PROTECTION: _message(403, "Upgrade to GitHub Pro or make this repository public."),
            CONTENTS: _message(409, "Git Repository is empty."),
        }
    )
    kinds = [e.event_type for e in events]
    assert kinds[-1] == "org.pull.completed"  # the pull went on to the end
    assert "repo.branch_protection.snapshot" not in kinds  # not visible: no fact either way
    (listing,) = [e for e in events if e.event_type == "repo.file_listing.snapshot"]
    assert listing.payload["files"] == []  # an empty repository has no files: a fact
    outcomes = report.outcomes()
    assert outcomes["repo_settings"].status == "partial"
    assert "branch protection not visible" in (outcomes["repo_settings"].detail or "")
    assert outcomes["collaborators"].status == "complete"
    assert report.status() == "partial"
    assert events[-1].payload["repo_collaborators"] == {"R_1": ["U_ANA"]}  # still settled


def test_collaborators_that_need_push_access_are_not_visible_not_fatal() -> None:
    events, report = _pull({COLLABORATORS: _message(403, "Must have push access.")})
    assert report.outcomes()["collaborators"].status == "not_visible"
    assert events[-1].payload["repo_collaborators"] == {}  # nothing settled for that repo
    assert events[-1].payload["members_listed"] is True


def test_a_refused_credential_stops_the_pull_as_an_auth_failure() -> None:
    with pytest.raises(GitHubPullError, match="HTTP 401") as refused:
        _pull({MEMBERS: _message(401, "Bad credentials")})
    assert refused.value.kind == "auth"
    with pytest.raises(GitHubPullError) as sso:
        _pull({MEMBERS: _message(403, "Resource protected by organization SAML enforcement.")})
    assert sso.value.kind == "auth"


def test_an_unknown_client_error_stops_the_pull_instead_of_reading_as_absent() -> None:
    with pytest.raises(GitHubPullError, match="HTTP 422") as caught:
        _pull({PROTECTION: _message(422, "Unprocessable")})
    assert caught.value.kind == "fail"


def test_an_organization_out_of_sight_is_not_visible() -> None:
    with pytest.raises(GitHubPullError) as caught:
        _pull({f"/orgs/{ORG}": _message(404, "Not Found")})
    assert caught.value.kind == "not_visible"


def test_every_request_spends_from_the_connections_bucket() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    bucket = RateBucket(rate=1.0, burst=2, clock=lambda: now[0], sleep=sleep)
    handler, seen = _api(["ana"])
    connector = GitHubLiveConnector(
        "t", transport=httpx.MockTransport(handler), sleep=sleep, bucket=bucket
    )
    list(connector.pull(TenantContext.for_id(Settings(), "acme")))
    assert len(seen) > 2
    assert bucket.waited == pytest.approx(len(seen) - 2)  # a burst of two, then one a second
    assert slept == [pytest.approx(1.0)] * (len(seen) - 2)
