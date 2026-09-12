"""The connector contract (spec.md Layer 1).

A connector turns a provider's data into ``SourceEvent`` objects. The dedup key is the
provider's own event id and the ordering key is the provider's own timestamp; nothing
locally generated is ever used for either. Payloads are passed through untouched so the
raw zone (Layer 2) is a faithful copy of what the provider said.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from ccp.tenancy import TenantContext, TenantId

ConnectorName = Literal["github", "hris", "manual"]
SourceKind = Literal["fixture", "live"]


class SourceEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: TenantId
    connector: ConnectorName
    source_event_id: str
    source_ts: datetime
    event_type: str
    payload: dict[str, Any]
    schema_version: str

    @field_validator("source_ts")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("source_ts must be timezone-aware")
        return value.astimezone(UTC)

    @property
    def dedup_key(self) -> tuple[str, str, str]:
        return (self.tenant_id, self.connector, self.source_event_id)


class Connector(Protocol):
    name: ConnectorName

    def pull(
        self, tenant: TenantContext, since: datetime | None = None
    ) -> Iterator[SourceEvent]: ...


def parse_ts(value: str) -> datetime:
    """Parse an ISO 8601 timestamp; a trailing Z means UTC."""
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no timezone")
    return ts.astimezone(UTC)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_bytes(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def after(events: Iterator[SourceEvent], since: datetime | None) -> Iterator[SourceEvent]:
    if since is None:
        yield from events
        return
    for event in events:
        if event.source_ts > since:
            yield event
