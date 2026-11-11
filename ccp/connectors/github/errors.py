"""What a GitHub response means, endpoint by endpoint (spec.md A-120).

Before, the client decided by status alone: a 404 was "not there" on an optional read and "not
visible" on a listing, and every other 4xx stopped the whole pull. So one private repository
on a plan without branch protection (403), one repository whose collaborators need push
access to list (403), or one empty repository (409 on its contents) ended the pull for the
whole organization. The table names what each status means on each endpoint, and the client
acts on the meaning, never on the status:

    ok            the body is the answer
    absent        the thing does not exist, which is a fact: an unprotected branch, no file
    not_visible   this credential cannot see it: no fact, and the scope is not complete
    retry         transient: back off and try again
    rate_limited  wait for the limit to reset, then try again
    auth          the credential is refused: stop; the connection needs attention
    fail          anything else: stop the pull

A status no rule names falls to the defaults at the bottom, and an unknown 4xx is a failure,
never an absence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

Meaning = Literal["ok", "absent", "not_visible", "retry", "rate_limited", "auth", "fail"]

ORG = "/orgs/{org}"
MEMBERS = "/orgs/{org}/members"
REPOS = "/orgs/{org}/repos"
COLLABORATORS = "/repos/{repo}/collaborators"
PROTECTION = "/repos/{repo}/branches/{branch}/protection"
CONTENTS = "/repos/{repo}/contents"
FILE = "/repos/{repo}/contents/{path}"
GRAPHQL = "/graphql"
CODE_SEARCH = "/search/code"  # a lookup about a failing resource (A-158)
ENDPOINTS = (ORG, MEMBERS, REPOS, COLLABORATORS, PROTECTION, CONTENTS, FILE, GRAPHQL, CODE_SEARCH)
ANY = "*"


@dataclass(frozen=True)
class Rule:
    endpoint: str  # one of ENDPOINTS, or ANY
    status: int
    meaning: Meaning
    why: str
    message: str | None = None  # matched, ignoring case, in the response body's "message"


TABLE: tuple[Rule, ...] = (
    # a credential not authorized for an organization that enforces single sign-on
    Rule(ANY, 403, "auth", "the credential is not authorized for this organization's SSO", "saml"),
    Rule(
        ORG, 404, "not_visible", "the organization does not exist or this credential cannot see it"
    ),
    Rule(MEMBERS, 404, "not_visible", "the member listing is hidden from this credential"),
    Rule(MEMBERS, 403, "not_visible", "the member listing is hidden from this credential"),
    Rule(REPOS, 404, "not_visible", "the repository listing is hidden from this credential"),
    Rule(REPOS, 403, "not_visible", "the repository listing is hidden from this credential"),
    Rule(COLLABORATORS, 404, "not_visible", "the collaborators are hidden from this credential"),
    Rule(COLLABORATORS, 403, "not_visible", "listing collaborators needs push access"),
    Rule(PROTECTION, 404, "absent", "the branch is not protected"),
    Rule(
        PROTECTION,
        403,
        "not_visible",
        "branch protection is not readable on this plan or credential",
    ),
    Rule(CONTENTS, 404, "absent", "the repository has no files"),
    Rule(CONTENTS, 409, "absent", "the repository is empty"),
    Rule(CONTENTS, 403, "not_visible", "the repository's files are hidden from this credential"),
    Rule(CONTENTS, 451, "not_visible", "the repository is unavailable for legal reasons"),
    Rule(FILE, 404, "absent", "no such file"),
    Rule(FILE, 403, "not_visible", "the file is hidden from this credential"),
    Rule(FILE, 451, "not_visible", "the file is unavailable for legal reasons"),
    Rule(CODE_SEARCH, 422, "absent", "the search cannot run over this organization's code"),
    Rule(CODE_SEARCH, 403, "not_visible", "code search is hidden from this credential"),
)

RETRY_STATUSES = frozenset({500, 502, 503, 504})


def is_rate_limit(response: httpx.Response) -> bool:
    """GitHub signals a limit with 429, or with 403 and no remaining quota or a Retry-After."""
    if response.status_code == 429:
        return True
    return response.status_code == 403 and (
        response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers
    )


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("message") or "").lower() if isinstance(body, dict) else ""


def classify(endpoint: str, response: httpx.Response) -> tuple[Meaning, str]:
    """The meaning of one response on one endpoint, and why."""
    status = response.status_code
    if 200 <= status < 300:
        return "ok", ""
    if is_rate_limit(response):
        return "rate_limited", f"rate limited ({status})"
    message = _message(response)
    for rule in TABLE:
        if rule.status != status or rule.endpoint not in (endpoint, ANY):
            continue
        if rule.message is not None and rule.message not in message:
            continue
        return rule.meaning, rule.why
    if status in RETRY_STATUSES:
        return "retry", f"HTTP {status}"
    if status == 401:
        return "auth", "the credential was refused"
    return "fail", f"HTTP {status}"
