"""Live HR data through an aggregator (spec.md A-141): Merge's HRIS API, read behind the
same sync contract as every connector (A-118).

`GET /api/hris/v1/employees` pages by cursor. Each employee becomes the same
`hris.employee.snapshot` the fixture lands, so the mapper, identity resolution and HR-1
need nothing new: the employee number when the HR system has one (else the aggregator's
id), the work email, the status (an inactive employee with a termination date is
terminated), and the start and termination dates. A page that fails stops the pull and
marks `employees` failed, so no PASS rests on a partial roster. Every response is kept with
the aggregator's request id (A-136).
"""

from __future__ import annotations

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
    parse_ts,
)
from ccp.tenancy import TenantContext

SCHEMA_VERSION = "merge.hris.v1"


def employee_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """An aggregator employee in the fixture's shape."""
    terminated = raw.get("employment_status") == "INACTIVE" and bool(raw.get("termination_date"))
    name = raw.get("display_full_name") or " ".join(
        p for p in (raw.get("first_name"), raw.get("last_name")) if p
    )
    updated = raw.get("modified_at") or raw.get("remote_updated_at")
    return {
        "employee_id": str(raw.get("employee_number") or raw["id"]),
        "display_name": name,
        "work_email": raw.get("work_email"),
        "department": None,
        "title": raw.get("job_title"),
        "manager_id": None,
        "employment_type": "employee",
        "status": "terminated" if terminated else "active",
        "hired_at": (raw.get("start_date") or "")[:10] or None,
        "terminated_at": (raw.get("termination_date") or "")[:10] or None,
        "updated_at": updated,
        "aggregator_id": raw["id"],
    }


class HrisAggregatorConnector:
    name: ConnectorName = "hris"
    source: SourceKind = "live"
    scopes: tuple[str, ...] = ("employees",)

    def __init__(
        self,
        api_key: str,
        account_token: str,
        base_url: str = "https://api.merge.dev",
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "X-Account-Token": account_token},
            timeout=30.0,
            transport=transport,
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self.responses: list[Received] = []

    def _page(self, cursor: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": 100}
        if cursor:
            params["cursor"] = cursor
        response = self._client.get("/api/hris/v1/employees", params=params)
        self.responses.append(
            Received(
                "GET",
                response.request.url.raw_path.decode(),
                response.status_code,
                response.headers.get("x-request-id"),
                response.content,
                self._clock(),
            )
        )
        if response.status_code != 200:
            kind = "auth" if response.status_code in (401, 403) else "fail"
            raise PullError(f"employees page: HTTP {response.status_code}", kind)  # type: ignore[arg-type]
        body: dict[str, Any] = response.json()
        return body

    def check(self, tenant: TenantContext) -> tuple[str, str]:
        try:
            self._page(None)
        except PullError as exc:
            return exc.kind, str(exc)
        return "ok", "the HR aggregator answers"

    def pull(
        self,
        tenant: TenantContext,
        since: datetime | None = None,
        report: SyncReport | None = None,
    ) -> Iterator[SourceEvent]:
        report = report or SyncReport(self.scopes)
        self.responses = []
        employees: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:  # every page, or the pull stops here and the scope is failed
            page = self._page(cursor)
            employees.extend(page.get("results") or [])
            cursor = page.get("next")
            if not cursor:
                break
        report.complete("employees", len(employees))
        for raw in employees:
            payload = employee_payload(raw)
            stamp = payload["updated_at"] or self._clock().isoformat()
            yield SourceEvent(
                tenant_id=tenant.tenant_id,
                connector="hris",
                source_event_id=f"{payload['employee_id']}@{stamp}",
                source_ts=parse_ts(stamp) if payload["updated_at"] else self._clock(),
                event_type="hris.employee.snapshot",
                payload=payload,
                schema_version=SCHEMA_VERSION,
            )
