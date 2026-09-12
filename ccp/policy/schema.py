"""GapReport and PolicyDraft records (spec.md Layer 15) and their persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from ccp.evaluators.verdict import Citation
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantId


class PolicyGap(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement_id: str  # "soc2:CC7.1"
    covered: bool
    covering_citations: tuple[Citation, ...] = ()  # verified; empty when not covered
    gap_description: str | None = None


class GapReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    report_id: str = Field(default_factory=lambda: str(ULID()))
    tenant_id: TenantId
    control_id: str
    gaps: tuple[PolicyGap, ...]
    summary: str
    confidence: float = Field(ge=0, le=1)
    confidence_cap: float = Field(ge=0, le=1)
    model: str | None
    prompt_version: str
    guardrail_events: tuple[str, ...] = ()
    thread_id: str | None = None
    request_json: dict[str, Any] | None = None
    response_json: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def uncovered(self) -> tuple[str, ...]:
        return tuple(g.requirement_id for g in self.gaps if not g.covered)


class PolicyDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft_id: str = Field(default_factory=lambda: str(ULID()))
    tenant_id: TenantId
    control_id: str
    based_on_report_id: str
    markdown: str
    addressed_requirements: tuple[str, ...]
    path: str  # set by the runner, not the graph
    approval_id: str  # set by the runner
    scrub_events: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


_REPORT_COLUMNS = (
    "report_id, tenant_id, control_id, gaps, summary, confidence, confidence_cap, model,"
    " prompt_version, guardrail_events, thread_id, request_json, response_json, created_at"
)


def persist_report(db: ScopedDB, report: GapReport) -> None:
    db.execute(
        f"INSERT INTO gap_reports ({_REPORT_COLUMNS})"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            report.report_id,
            report.tenant_id,
            report.control_id,
            json.dumps([g.model_dump(mode="json") for g in report.gaps]),
            report.summary,
            report.confidence,
            report.confidence_cap,
            report.model,
            report.prompt_version,
            json.dumps(list(report.guardrail_events)),
            report.thread_id,
            json.dumps(report.request_json, default=str) if report.request_json else None,
            json.dumps(report.response_json, default=str) if report.response_json else None,
            report.created_at,
        ),
    )


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _report(r: tuple[Any, ...]) -> GapReport:
    return GapReport(
        report_id=str(r[0]),
        tenant_id=TenantId(str(r[1])),
        control_id=str(r[2]),
        gaps=tuple(PolicyGap.model_validate(g) for g in _load(r[3])),
        summary=str(r[4]),
        confidence=float(r[5]),
        confidence_cap=float(r[6]),
        model=r[7],
        prompt_version=str(r[8]),
        guardrail_events=tuple(_load(r[9])),
        thread_id=r[10],
        request_json=_load(r[11]) if r[11] is not None else None,
        response_json=_load(r[12]) if r[12] is not None else None,
        created_at=r[13],
    )


def latest_report(db: ScopedDB, control_id: str) -> GapReport | None:
    row = db.fetch_one(
        f"SELECT {_REPORT_COLUMNS} FROM gap_reports WHERE control_id = %s"
        " ORDER BY created_at DESC LIMIT 1",
        (control_id,),
    )
    return _report(row) if row else None


def get_report(db: ScopedDB, report_id: str) -> GapReport | None:
    row = db.fetch_one(
        f"SELECT {_REPORT_COLUMNS} FROM gap_reports WHERE report_id = %s", (report_id,)
    )
    return _report(row) if row else None


def persist_draft(db: ScopedDB, draft: PolicyDraft, markdown_sha256: str) -> None:
    db.execute(
        "INSERT INTO policy_drafts (draft_id, tenant_id, control_id, based_on_report_id, path,"
        " markdown_sha256, addressed_requirements, approval_id, scrub_events, created_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            draft.draft_id,
            draft.tenant_id,
            draft.control_id,
            draft.based_on_report_id,
            draft.path,
            markdown_sha256,
            json.dumps(list(draft.addressed_requirements)),
            draft.approval_id,
            json.dumps(list(draft.scrub_events)),
            draft.created_at,
        ),
    )


def list_drafts(db: ScopedDB, control_id: str | None = None) -> list[dict[str, Any]]:
    query = (
        "SELECT draft_id, control_id, based_on_report_id, path, approval_id,"
        " addressed_requirements, scrub_events, created_at FROM policy_drafts"
    )
    params: tuple[Any, ...] = ()
    if control_id:
        query += " WHERE control_id = %s"
        params = (control_id,)
    rows = db.fetch_all(query + " ORDER BY created_at DESC", params)
    return [
        {
            "draft_id": str(r[0]),
            "control_id": str(r[1]),
            "based_on_report_id": str(r[2]),
            "path": str(r[3]),
            "approval_id": str(r[4]),
            "addressed_requirements": list(_load(r[5])),
            "scrub_events": list(_load(r[6])),
            "created_at": r[7].isoformat(),
        }
        for r in rows
    ]
