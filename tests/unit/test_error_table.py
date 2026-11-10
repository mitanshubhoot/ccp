"""MT1 (A-120): the per-endpoint error table and the rate buckets, without a network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from ccp.connectors import rate
from ccp.connectors.github import client, errors
from ccp.connectors.github.errors import (
    COLLABORATORS,
    CONTENTS,
    FILE,
    GRAPHQL,
    MEMBERS,
    ORG,
    PROTECTION,
    REPOS,
)
from ccp.connectors.rate import RateBucket

PLAN = "Upgrade to GitHub Pro or make this repository public to enable this feature."
SAML = (
    "Resource protected by organization SAML enforcement. You must grant your Personal"
    " Access token access to this organization."
)


@pytest.mark.parametrize(
    ("endpoint", "status", "body", "headers", "meaning"),
    [
        (PROTECTION, 404, {"message": "Branch not protected"}, {}, "absent"),
        (PROTECTION, 403, {"message": PLAN}, {}, "not_visible"),
        (CONTENTS, 404, {"message": "This repository is empty."}, {}, "absent"),
        (CONTENTS, 409, {"message": "Git Repository is empty."}, {}, "absent"),
        (FILE, 404, {"message": "Not Found"}, {}, "absent"),
        (
            COLLABORATORS,
            403,
            {"message": "Must have push access to view collaborators."},
            {},
            "not_visible",
        ),
        (COLLABORATORS, 404, {"message": "Not Found"}, {}, "not_visible"),
        (MEMBERS, 404, {"message": "Not Found"}, {}, "not_visible"),
        (REPOS, 403, {"message": "Forbidden"}, {}, "not_visible"),
        (ORG, 404, {"message": "Not Found"}, {}, "not_visible"),
        (MEMBERS, 403, {"message": SAML}, {}, "auth"),  # SSO: the credential needs authorizing
        (FILE, 403, {"message": SAML}, {}, "auth"),
        (MEMBERS, 401, {"message": "Bad credentials"}, {}, "auth"),
        (GRAPHQL, 401, {"message": "Bad credentials"}, {}, "auth"),
        (
            FILE,
            403,
            {"message": "API rate limit exceeded"},
            {"x-ratelimit-remaining": "0"},
            "rate_limited",
        ),
        (
            PROTECTION,
            403,
            {"message": "secondary rate limit"},
            {"retry-after": "30"},
            "rate_limited",
        ),
        (REPOS, 429, {}, {}, "rate_limited"),
        (GRAPHQL, 502, {}, {}, "retry"),
        (COLLABORATORS, 503, {}, {}, "retry"),
        (PROTECTION, 422, {"message": "Unprocessable"}, {}, "fail"),  # never read as absent
        (GRAPHQL, 404, {}, {}, "fail"),
        (ORG, 200, {"login": "acme"}, {}, "ok"),
    ],
)
def test_the_table_decides_by_endpoint_not_by_status(
    endpoint: str, status: int, body: Any, headers: dict[str, str], meaning: str
) -> None:
    response = httpx.Response(status, json=body, headers=headers)
    assert errors.classify(endpoint, response)[0] == meaning


def test_a_body_that_is_not_json_still_classifies() -> None:
    assert errors.classify(PROTECTION, httpx.Response(403, text="<html>"))[0] == "not_visible"


def test_every_rule_names_an_endpoint_the_client_calls() -> None:
    source = Path(client.__file__).read_text(encoding="utf-8")
    names = {value: name for name, value in vars(errors).items() if isinstance(value, str)}
    for rule in errors.TABLE:
        assert rule.endpoint == errors.ANY or rule.endpoint in errors.ENDPOINTS
    for endpoint in errors.ENDPOINTS:
        assert f"errors.{names[endpoint]}" in source, endpoint  # the client uses each one


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_a_bucket_spends_its_burst_then_waits_at_its_rate() -> None:
    clock = _Clock()
    bucket = RateBucket(rate=2.0, burst=3, clock=clock, sleep=clock.sleep)
    assert [bucket.take() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert bucket.take() == pytest.approx(0.5)  # the fourth waits for a token at 2 a second
    assert bucket.take() == pytest.approx(0.5)
    clock.now += 60
    assert bucket.take() == 0.0  # refilled, but never beyond the burst
    assert [bucket.take() for _ in range(2)] == [0.0, 0.0]
    assert bucket.take() == pytest.approx(0.5)


def test_the_providers_count_holds_the_bucket_until_the_reset() -> None:
    clock = _Clock()
    bucket = RateBucket(rate=100.0, burst=10, reserve=5, clock=clock, sleep=clock.sleep)
    bucket.observe(remaining=50, reset_in=30.0)
    assert bucket.take() == 0.0  # plenty left
    bucket.observe(remaining=5, reset_in=30.0)
    assert bucket.take() == pytest.approx(30.0)  # down to the reserve: wait for the reset
    assert bucket.take() == 0.0
    assert bucket.waited == pytest.approx(30.0)


def test_one_bucket_per_tenant_and_connection() -> None:
    first = rate.bucket_for("acme", "github:acme-example", lambda: RateBucket(1.0, 1))
    assert rate.bucket_for("acme", "github:acme-example", lambda: RateBucket(9.0, 9)) is first
    assert rate.bucket_for("globex", "github:acme-example", lambda: RateBucket(1.0, 1)) is not first
    assert rate.bucket_for("acme", "github:other", lambda: RateBucket(1.0, 1)) is not first
    rate.reset()
    assert rate.bucket_for("acme", "github:acme-example", lambda: RateBucket(1.0, 1)) is not first
