"""Canonical entities (spec.md Layer 3).

Every entity carries provenance back to the raw event that produced it. Entity ids are
deterministic (``{connector}:{kind}:{source_native_id}``) so repeated observations of the
same thing land on the same id and "current" is simply the latest observation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ccp.tenancy import TenantId
from ccp.types import AccountKind, EmploymentStatus, EvidenceType, GrantState

EntityKind = Literal["person", "access_grant", "asset", "finding", "evidence", "vendor"]


class CanonicalBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    entity_id: str
    source_connector: str
    source_event_id: str
    observed_at: datetime

    @property
    def kind(self) -> EntityKind:
        raise NotImplementedError


class Person(CanonicalBase):
    """A person or account as one source saw it.

    Observations may be partial (an HR termination event carries only the status). The
    repository merges observations per field in time order; a null or empty value never
    overrides an earlier known one, so a partial event cannot blank out a full snapshot.
    """

    display_name: str | None = None
    emails: tuple[str, ...] = ()
    handles: dict[str, str] = Field(default_factory=dict)
    employee_id: str | None = None
    account_kind: AccountKind = AccountKind.unknown
    employment_status: EmploymentStatus = EmploymentStatus.unknown
    status_effective_at: datetime | None = None
    owner_employee_id: str | None = None  # for shared and service accounts

    @property
    def kind(self) -> EntityKind:
        return "person"


class AccessGrant(CanonicalBase):
    person_entity_id: str
    asset_entity_id: str
    role: str
    state: GrantState
    state_effective_at: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)

    @property
    def kind(self) -> EntityKind:
        return "access_grant"


class Asset(CanonicalBase):
    name: str
    asset_type: str
    tags: tuple[str, ...] = ()
    settings: dict[str, Any] = Field(default_factory=dict)

    @property
    def kind(self) -> EntityKind:
        return "asset"


class Finding(CanonicalBase):
    test_id: str
    severity: str
    summary: str
    subject_entity_ids: tuple[str, ...] = ()

    @property
    def kind(self) -> EntityKind:
        return "finding"


class Evidence(CanonicalBase):
    evidence_type: EvidenceType
    title: str
    text: str
    subject_entity_id: str | None = None
    issued_at: datetime | None = None
    raw_ref: str
    page_texts: tuple[str, ...] | None = None  # PDFs: one entry per page, for page-aware chunking
    guardrail_events: tuple[str, ...] = ()
    # how each page's text was obtained (A-147): text_layer, markdown, transcribed:<model>:<v>,
    # blank:awaiting_transcription or illegible:<model>:<v>; one entry per page, or one
    page_methods: tuple[str, ...] | None = None

    @property
    def kind(self) -> EntityKind:
        return "evidence"


class Vendor(CanonicalBase):
    name: str
    vendor_type: str
    data_categories: tuple[str, ...] = ()

    @property
    def kind(self) -> EntityKind:
        return "vendor"


Entity = Person | AccessGrant | Asset | Finding | Evidence | Vendor


class MappingResult(BaseModel):
    """What a mapper returns: entities plus any guardrail events raised while mapping."""

    model_config = ConfigDict(frozen=True)

    entities: tuple[Entity, ...] = ()
    guardrail_events: tuple[str, ...] = ()

    def of(self, cls: type[Any]) -> list[Any]:
        return [e for e in self.entities if isinstance(e, cls)]
