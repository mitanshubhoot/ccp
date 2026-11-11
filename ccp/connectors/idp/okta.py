"""The Okta connector (spec.md A-141).

    users     every user, by Okta's immutable id (`00u...`): a renamed login or a changed
              email is the same user, so a person's history never splits in two
    factors   each user's enrolled factors; MFA is any factor that is ACTIVE

`GET /api/v1/users` pages through the Link header; a page that fails stops the pull, and
the scope is marked failed, so a PASS that rests on "every user" is withheld (A-123). A
deprovisioned or suspended user is still landed, with its status, so the mapper can say
their access ended. Every response is kept with Okta's request id (A-136).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from ccp.connectors.base import (
    ConnectorName,
    PullError,
    Received,
    SourceEvent,
    SourceKind,
    SyncReport,
)
from ccp.connectors.github.client import content_id
from ccp.tenancy import TenantContext

_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
PAGE = 200


def _kind(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "not_visible"
    return "fail"


class OktaConnector:
    name: ConnectorName = "idp"
    source: SourceKind = "live"
    scopes: tuple[str, ...] = ("users", "factors")

    def __init__(
        self,
        domain: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        base = domain if domain.startswith("http") else f"https://{domain}"
        self.domain = base.removeprefix("https://").removeprefix("http://").rstrip("/")
        self._client = httpx.Client(
            base_url=base,
            headers={"Authorization": f"SSWS {token}", "Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self.responses: list[Received] = []

    def _get(self, url: str, **params: Any) -> httpx.Response:
        response = self._client.get(url, params=params or None)
        self.responses.append(
            Received(
                "GET",
                response.request.url.raw_path.decode(),
                response.status_code,
                response.headers.get("x-okta-request-id"),
                response.content,
                self._clock(),
            )
        )
        if response.status_code != 200:
            raise PullError(
                f"GET {url}: HTTP {response.status_code}",
                _kind(response.status_code),  # type: ignore[arg-type]
            )
        return response

    def check(self, tenant: TenantContext) -> tuple[str, str]:
        try:
            self._get("/api/v1/users", limit=1)
        except PullError as exc:
            return exc.kind, str(exc)
        return "ok", f"Okta {self.domain} answers"

    def _users(self) -> Iterator[dict[str, Any]]:
        url: str | None = "/api/v1/users"
        params: dict[str, Any] = {"limit": PAGE}
        while url is not None:
            response = self._get(url, **params)
            yield from response.json()
            found = _NEXT.search(response.headers.get("link", ""))
            url, params = (found.group(1), {}) if found else (None, {})

    def pull(
        self,
        tenant: TenantContext,
        since: datetime | None = None,
        report: SyncReport | None = None,
    ) -> Iterator[SourceEvent]:
        report = report or SyncReport(self.scopes)
        self.responses = []
        users = list(self._users())  # every page, or the pull stops here
        report.complete("users", len(users))
        for user in users:
            factors = self._get(f"/api/v1/users/{user['id']}/factors").json()
            profile = user.get("profile") or {}
            payload = {
                "domain": self.domain,
                "user_id": user["id"],
                "status": user.get("status"),
                "login": profile.get("login"),
                "email": profile.get("email"),
                "display_name": " ".join(
                    p for p in (profile.get("firstName"), profile.get("lastName")) if p
                ),
                "factors": sorted(
                    {f.get("factorType", "?") for f in factors if f.get("status") == "ACTIVE"}
                ),
                "mfa_enabled": any(f.get("status") == "ACTIVE" for f in factors),
            }
            yield SourceEvent(
                tenant_id=tenant.tenant_id,
                connector="idp",
                source_event_id=content_id(f"okta:user:{user['id']}", payload),
                source_ts=self._clock(),
                event_type="idp.user.snapshot",
                payload=payload,
                schema_version="okta.v1",
            )
        report.complete("factors", len(users))
