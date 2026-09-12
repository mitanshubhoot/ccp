"""Investigation records (spec.md Layer 14): what the runner persists after verification and
bounding. The model-facing draft schema lives in ccp.evaluators.extraction.investigation
and is re-exported here."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from ccp.evaluators.extraction.investigation import (
    GATHER_PROMPT_VERSION,
    PROMPT_VERSION,
    Category,
    InvestigationDraft,
    MissingEvidence,
    ProposalTarget,
    ProposedAction,
)
from ccp.evaluators.verdict import Citation
from ccp.knowledge.actions import ActionName
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantId

__all__ = [
    "GATHER_PROMPT_VERSION",
    "PROMPT_VERSION",
    "Category",
    "Investigation",
    "InvestigationDraft",
    "MissingEvidence",
    "ProposalTarget",
    "ProposedAction",
    "RemediationProposal",
    "for_verdict",
    "get",
    "list_all",
    "persist",
]


class RemediationProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: ActionName
    target: dict[str, str]
    rationale: str
    reversible: bool  # from the catalogue, never the model
    live_capable: bool  # same
    proposal_id: str | None = None  # the remediation_proposals row, once written


class Investigation(BaseModel):
    model_config = ConfigDict(frozen=True)

    investigation_id: str = Field(default_factory=lambda: str(ULID()))
    tenant_id: TenantId
    verdict_id: str
    root_cause: str
    root_cause_category: Category
    missing_evidence: tuple[MissingEvidence, ...] = ()
    proposed_actions: tuple[RemediationProposal, ...] = ()
    citations: tuple[Citation, ...] = ()
    confidence: float = Field(ge=0, le=1)
    confidence_cap: float = Field(ge=0, le=1)
    model: str | None
    prompt_version: str
    guardrail_events: tuple[str, ...] = ()
    thread_id: str | None = None
    request_json: dict[str, Any] | None = None
    response_json: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


_COLUMNS = (
    "investigation_id, tenant_id, verdict_id, root_cause, root_cause_category, missing_evidence,"
    " proposed_actions, citations, confidence, confidence_cap, model, prompt_version,"
    " guardrail_events, thread_id, request_json, response_json, created_at"
)


def persist(db: ScopedDB, investigation: Investigation) -> None:
    db.execute(
        f"INSERT INTO investigations ({_COLUMNS})"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            investigation.investigation_id,
            investigation.tenant_id,
            investigation.verdict_id,
            investigation.root_cause,
            investigation.root_cause_category,
            json.dumps([m.model_dump(mode="json") for m in investigation.missing_evidence]),
            json.dumps([p.model_dump(mode="json") for p in investigation.proposed_actions]),
            json.dumps([c.model_dump(mode="json") for c in investigation.citations]),
            investigation.confidence,
            investigation.confidence_cap,
            investigation.model,
            investigation.prompt_version,
            json.dumps(list(investigation.guardrail_events)),
            investigation.thread_id,
            json.dumps(investigation.request_json, default=str)
            if investigation.request_json
            else None,
            json.dumps(investigation.response_json, default=str)
            if investigation.response_json
            else None,
            investigation.created_at,
        ),
    )


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _from_row(r: tuple[Any, ...]) -> Investigation:
    return Investigation(
        investigation_id=str(r[0]),
        tenant_id=TenantId(str(r[1])),
        verdict_id=str(r[2]),
        root_cause=str(r[3]),
        root_cause_category=r[4],
        missing_evidence=tuple(MissingEvidence.model_validate(m) for m in _load(r[5])),
        proposed_actions=tuple(RemediationProposal.model_validate(p) for p in _load(r[6])),
        citations=tuple(Citation.model_validate(c) for c in _load(r[7])),
        confidence=float(r[8]),
        confidence_cap=float(r[9]),
        model=r[10],
        prompt_version=str(r[11]),
        guardrail_events=tuple(_load(r[12])),
        thread_id=r[13],
        request_json=_load(r[14]) if r[14] is not None else None,
        response_json=_load(r[15]) if r[15] is not None else None,
        created_at=r[16],
    )


def for_verdict(db: ScopedDB, verdict_id: str) -> Investigation | None:
    row = db.fetch_one(
        f"SELECT {_COLUMNS} FROM investigations WHERE verdict_id = %s"
        " ORDER BY created_at DESC LIMIT 1",
        (verdict_id,),
    )
    return _from_row(row) if row else None


def get(db: ScopedDB, investigation_id: str) -> Investigation | None:
    row = db.fetch_one(
        f"SELECT {_COLUMNS} FROM investigations WHERE investigation_id = %s", (investigation_id,)
    )
    return _from_row(row) if row else None


def list_all(db: ScopedDB, limit: int = 50) -> list[Investigation]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM investigations ORDER BY created_at DESC LIMIT %s", (limit,)
    )
    return [_from_row(r) for r in rows]
