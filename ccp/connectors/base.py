"""The connector contract (spec.md Layer 1, A-118).

A connector turns a provider's data into ``SourceEvent`` objects. The dedup key is the
provider's own event id and the ordering key is the provider's own timestamp; nothing
locally generated is ever used for either. Payloads are passed through untouched so the
raw zone (Layer 2) is a faithful copy of what the provider said.

A pull also says what it saw (A-118). A connector declares its scopes, the data types it
reads, and marks each one in the run's `SyncReport`: complete (every listing seen in full,
so an absence there is a fact), partial (some of it seen), not visible (the credential
cannot see it), or failed. A scope the pull never reached counts as failed, and the run is
complete only when every scope is.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from ccp.tenancy import TenantContext, TenantId

ConnectorName = Literal["github", "hris", "manual", "aws", "idp"]
SourceKind = Literal["fixture", "live"]
ScopeStatus = Literal["complete", "partial", "not_visible", "failed"]
RunStatus = Literal["complete", "partial", "failed"]
PullErrorKind = Literal["auth", "not_visible", "fail"]


class PullError(RuntimeError):
    """A pull that cannot go on. Its kind says why, in the error table's terms (A-120): the
    credential was refused (auth), the source is hidden from it (not_visible), or anything
    else (fail). The sync run it ends records the kind."""

    def __init__(self, message: str, kind: PullErrorKind = "fail") -> None:
        super().__init__(message)
        self.kind: PullErrorKind = kind


@dataclass(frozen=True)
class Connection:
    """One connector reading one provider account (A-121), as declared and versioned in the
    `connections` table. `credential` is the purpose name of the secret it reads."""

    connection_id: str
    version: int
    connector: ConnectorName
    source: SourceKind
    account: str
    credential: str | None
    enabled: bool
    # what a connector needs beyond its account, found by CCP, never a secret: for AWS the
    # verified role and CCP's external ID (A-139)
    params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ScopeOutcome:
    status: ScopeStatus
    items: int = 0
    detail: str | None = None


class SyncReport:
    """What one pull saw, scope by scope (A-118)."""

    def __init__(self, scopes: Sequence[str]) -> None:
        self.declared = tuple(scopes)
        self._outcomes: dict[str, ScopeOutcome] = {}
        self.error: str | None = None  # set when the pull itself raised

    def mark(
        self, scope: str, status: ScopeStatus, items: int = 0, detail: str | None = None
    ) -> None:
        if scope not in self.declared:
            raise ValueError(f"scope {scope!r} is not declared ({', '.join(self.declared)})")
        self._outcomes[scope] = ScopeOutcome(status, items, detail)

    def complete(self, scope: str, items: int = 0) -> None:
        self.mark(scope, "complete", items)

    def outcomes(self) -> dict[str, ScopeOutcome]:
        """Every declared scope; one the pull never reached is failed."""
        unreached = ScopeOutcome("failed", 0, "the pull stopped before this scope")
        return {s: self._outcomes.get(s, unreached) for s in self.declared}

    def status(self) -> RunStatus:
        outcomes = self.outcomes().values()
        if self.error is not None or any(o.status == "failed" for o in outcomes):
            return "failed"
        if all(o.status == "complete" for o in outcomes):
            return "complete"
        return "partial"


@dataclass(frozen=True)
class Received:
    """One response a connector received, as bytes, with the provider's request id (A-136):
    landing keeps it in the raw zone as sent, beside the events made from it."""

    method: str
    path: str
    status: int
    request_id: str | None
    body: bytes
    received_at: datetime

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


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
    source: SourceKind
    scopes: tuple[str, ...]  # the data types a pull reads, each marked in its SyncReport

    def pull(
        self,
        tenant: TenantContext,
        since: datetime | None = None,
        report: SyncReport | None = None,
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
