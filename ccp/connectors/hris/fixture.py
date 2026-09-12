"""HRIS fixture reader (the scheduled/document path).

Reads ``fixtures/{tenant}/hris/`` and emits:
  hris.employee.snapshot   roster rows, id f"{employee_id}@{updated_at}"
  hris.hire / hris.role_change / hris.termination   from events.json, id = the HR event id
  hris.identity.snapshot   SSO identities with MFA status and account kind
  hris.account.snapshot    the account registry (shared and service accounts, owners)
  hris.asset.snapshot      systems with tags
  hris.document            documents/*.md, id f"{document_type}:{filename}:{sha256[:12]}",
                           ts = issued_at
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ccp.connectors.base import (
    ConnectorName,
    SourceEvent,
    after,
    load_json,
    parse_ts,
    sha256_bytes,
)
from ccp.connectors.frontmatter import split_front_matter
from ccp.tenancy import TenantContext

SCHEMA_VERSION = "hris.v1"
_EVENT_TYPES = {
    "hire": "hris.hire",
    "role_change": "hris.role_change",
    "termination": "hris.termination",
}


class HrisFixtureConnector:
    name: ConnectorName = "hris"

    def __init__(self, fixtures_dir: Path) -> None:
        self._fixtures_dir = fixtures_dir

    def pull(self, tenant: TenantContext, since: datetime | None = None) -> Iterator[SourceEvent]:
        return after(self._events(tenant), since)

    def _events(self, tenant: TenantContext) -> Iterator[SourceEvent]:
        root = self._fixtures_dir / tenant.tenant_id / "hris"
        if not root.is_dir():
            raise FileNotFoundError(f"no HRIS fixture for tenant {tenant.tenant_id!r} at {root}")

        def event(
            event_type: str, source_event_id: str, ts: str, payload: dict[str, Any]
        ) -> SourceEvent:
            return SourceEvent(
                tenant_id=tenant.tenant_id,
                connector="hris",
                source_event_id=source_event_id,
                source_ts=parse_ts(ts),
                event_type=event_type,
                payload=payload,
                schema_version=SCHEMA_VERSION,
            )

        for employee in load_json(root / "employees.json"):
            yield event(
                "hris.employee.snapshot",
                f"{employee['employee_id']}@{employee['updated_at']}",
                employee["updated_at"],
                employee,
            )
        for hr_event in load_json(root / "events.json"):
            yield event(
                _EVENT_TYPES[hr_event["type"]],
                hr_event["event_id"],
                hr_event["effective_at"],
                hr_event,
            )
        for identity in load_json(root / "identities.json"):
            yield event(
                "hris.identity.snapshot",
                f"{identity['identity_id']}@{identity['updated_at']}",
                identity["updated_at"],
                identity,
            )
        for account in load_json(root / "accounts.json"):
            yield event(
                "hris.account.snapshot",
                f"{account['account_id']}@{account['updated_at']}",
                account["updated_at"],
                account,
            )
        for asset in load_json(root / "assets.json"):
            yield event(
                "hris.asset.snapshot",
                f"{asset['asset_id']}@{asset['updated_at']}",
                asset["updated_at"],
                asset,
            )
        for path in sorted((root / "documents").glob("*.md")):
            raw = path.read_bytes()
            front_matter, body = split_front_matter(raw.decode("utf-8"))
            document_type = str(front_matter["document_type"])
            issued_at = str(front_matter["issued_at"])
            digest = sha256_bytes(raw)[:12]
            yield event(
                "hris.document",
                f"{document_type}:{path.name}:{digest}",
                issued_at,
                {
                    "filename": path.name,
                    "sha256": sha256_bytes(raw),
                    "front_matter": {
                        k: str(v) if not isinstance(v, str | int | float | bool | None) else v
                        for k, v in front_matter.items()
                    },
                    "document_type": document_type,
                    "subject_employee_id": front_matter.get("subject_employee_id"),
                    "title": front_matter.get("title"),
                    "issued_at": issued_at,
                    "text": body,
                },
            )
