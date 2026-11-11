"""The typed tool registry (spec.md Layer 8, A-112): the one source of truth for every tool.

Every tool takes its tenant from the closure created when the graph is built, never from
an argument the model can set: no tool schema has a `tenant_id` field and every schema
forbids extra fields, so a model passing one gets a validation error, not another tenant.
This module imports nothing with a side effect (tests/unit/test_layering.py); each tool
only reads through ScopedDB.

Each tool declares, besides its argument schema: an output schema the gateway checks every
result against; a class (read, propose or act, set here and never taken from a hint); the
tenant data it reads (scopes); whether its result can carry text a customer or a model
wrote (untrusted); a per-run call limit; a time limit; and a version. Absent data is an
error that says what is missing, so an empty list is never read as "nothing wrong".
`BoundTool.call` is the gateway every call goes through, whoever makes it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ccp.agent.authz import Decision, Gate
from ccp.evaluators import inventory
from ccp.identity import resolver
from ccp.knowledge import graph as knowledge
from ccp.knowledge import rollup as knowledge_rollup
from ccp.normalize import repository
from ccp.retrieval import documents, generations, hybrid
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import EmbeddingClient
from ccp.retrieval.rerank import Reranker
from ccp.security.secrets_patterns import redact
from ccp.storage import bitemporal
from ccp.storage import exceptions as stored_exceptions
from ccp.storage.scoped import ScopedDB
from ccp.types import EmploymentStatus, EvidenceType, GrantState

MAX_TOP_K = 8


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchEvidenceArgs(_Args):
    query: str = Field(min_length=1)
    evidence_type: EvidenceType
    top_k: int = Field(default=6, ge=1, le=MAX_TOP_K)
    subject_entity_id: str | None = None
    issued_after: date | None = Field(default=None, description="issued on or after")
    issued_before: date | None = Field(default=None, description="issued on or before")
    as_of: datetime | None = Field(default=None, description="search what the index held then")


class FindExactArgs(_Args):
    text: str | None = Field(default=None, min_length=2, max_length=200)
    kind: documents.Kind | None = Field(
        default=None, description="a typed pattern instead of text: cve, arn, email, date"
    )
    evidence_type: EvidenceType | None = None
    evidence_entity_id: str | None = None
    limit: int = Field(default=10, ge=1, le=25)

    @model_validator(mode="after")
    def _one_of(self) -> FindExactArgs:
        if (self.text is None) == (self.kind is None):
            raise ValueError("give exactly one of text and kind")
        return self


class OutlineArgs(_Args):
    evidence_type: EvidenceType
    evidence_entity_id: str
    version: str | None = Field(default=None, description="an observation id; default newest")


class ReadSpanArgs(_Args):
    evidence_type: EvidenceType
    evidence_entity_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    page: int | None = Field(default=None, ge=1, description="required for paged documents")
    version: str | None = None


class FindAccessArgs(_Args):
    employment_status: EmploymentStatus | None = None
    asset_prefix: str | None = Field(default=None, description="e.g. github: or aws:")
    role: str | None = None
    state: GrantState = GrantState.active
    limit: int = Field(default=25, ge=1, le=50)


class GetChunkArgs(_Args):
    chunk_id: str


class ListEvidenceArgs(_Args):
    evidence_type: EvidenceType


class GetEvidenceChunksArgs(_Args):
    evidence_entity_id: str


class ListAssetsArgs(_Args):
    tag: str | None = None


class GetPersonArgs(_Args):
    canonical_person_id: str


class GetCurrentStateArgs(_Args):
    test_id: str
    subject_key: str


class GetVerdictArgs(_Args):
    verdict_id: str


class ListFindingsArgs(_Args):
    status: str | None = None
    test_id: str | None = None


class GetAccessGrantsArgs(_Args):
    canonical_person_id: str


class GetControlArgs(_Args):
    control_id: str


class GetFrameworkRollupArgs(_Args):
    framework_id: str


class GetInvestigationArgs(_Args):
    verdict_id: str


# -- output schemas: what each tool returns; the gateway checks every result (A-112) -------


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChunkOut(_Out):
    chunk_id: str
    evidence_entity_id: str
    evidence_type: str
    subject_entity_id: str | None
    page: int | None
    source_ref: str
    text: str
    redactions: list[str]
    scores: dict[str, Any] | None = None
    start_offset: int | None = None
    end_offset: int | None = None


class SearchEvidenceOut(_Out):
    chunks: list[ChunkOut]
    events: list[str]
    embed_model: str | None
    rerank_model: str | None


class EvidenceRowOut(_Out):
    evidence_entity_id: str
    title: str | None
    issued_at: str
    subject_entity_id: str | None
    source_ref: str | None


class ListEvidenceOut(_Out):
    evidence: list[EvidenceRowOut]


class ChunksOut(_Out):
    chunks: list[ChunkOut]


class SpanRefOut(_Out):
    chunk_id: str
    page: int | None
    start_offset: int | None
    end_offset: int | None


class MatchOut(_Out):
    evidence_entity_id: str
    version: str
    evidence_type: str
    page: int | None
    start_offset: int
    end_offset: int
    match: str
    text: str  # the match with SNIPPET characters either side
    redactions: list[str]
    chunk_id: str | None


class FindExactOut(_Out):
    matches: list[MatchOut]
    total: int
    truncated: bool


class OutlineEntryOut(_Out):
    kind: Literal["heading", "table"]
    level: int
    title: str
    page: int | None
    start_offset: int
    end_offset: int


class OutlineOut(_Out):
    evidence_entity_id: str
    version: str
    evidence_type: str
    title: str
    observed_at: str
    pages: int | None
    length: int
    entries: list[OutlineEntryOut]
    chunks: list[SpanRefOut]


class ReadSpanOut(_Out):
    evidence_entity_id: str
    version: str
    evidence_type: str
    page: int | None
    start_offset: int
    end_offset: int
    truncated: bool
    text: str
    redactions: list[str]
    chunks: list[SpanRefOut]


class AccessRowOut(_Out):
    canonical_person_id: str
    display_name: str | None
    employment_status: str
    account_entity_id: str
    asset_entity_id: str
    role: str
    state: str
    state_effective_at: str
    observed_at: str


class FindAccessOut(_Out):
    rows: list[AccessRowOut]
    total: int
    truncated: bool
    read_at: str


class AssetOut(_Out):
    entity_id: str
    name: str
    asset_type: str
    tags: list[str]


class ListAssetsOut(_Out):
    assets: list[AssetOut]


class AccountOut(_Out):
    entity_id: str
    display_name: str | None
    emails: list[str]
    handles: dict[str, Any]
    employment_status: str
    account_kind: str


class PersonOut(_Out):
    canonical_person_id: str
    accounts: list[AccountOut]


class ExceptionOut(_Out):
    until: str
    owner: str
    reason: str


class StateOut(_Out):
    status: str
    shown_as: str  # "fail (excepted until D)" while an exception holds (A-132)
    exception: ExceptionOut | None = None
    valid_from: str
    verdict_id: str
    reason: str


class VerdictOut(_Out):
    verdict_id: str
    test_id: str
    subject_key: str
    status: str
    reasoning: str
    confidence: float
    tier: int
    model: str | None
    citations: list[dict[str, Any]]
    retrieved_chunk_ids: list[str]
    evidence_refs: list[str]


class FindingOut(_Out):
    subject_key: str
    test_id: str
    status: str
    shown_as: str  # still a failure when excepted (A-132)
    verdict_id: str


class FindingsOut(_Out):
    findings: list[FindingOut]


class GrantOut(_Out):
    entity_id: str
    asset_entity_id: str
    role: str | None
    state: str
    state_effective_at: str
    connector: str


class GrantsOut(_Out):
    grants: list[GrantOut]


class RequirementOut(_Out):
    framework: str
    requirement_id: str
    title: str
    intent: str


class ControlOut(_Out):
    control_id: str
    title: str
    description: str
    requirements: list[RequirementOut]
    tests: list[str]


class RollupRowOut(_Out):
    requirement_id: str
    state: str
    shown_as: str  # "fail (excepted until D)" when every failing subject is excepted (A-132)
    blocking_test_id: str | None
    blocking_subject_key: str | None
    counts: dict[str, int]
    pending_reasons: dict[str, int] = {}  # unassessed, parked, stale, ... (A-124)


class RollupOut(_Out):
    framework_id: str
    no_evaluations_yet: bool
    requirements: list[RollupRowOut]


class InvestigationOut(_Out):
    verdict_id: str
    investigation_id: str
    root_cause: str | None
    root_cause_category: str | None
    missing_evidence: Any
    proposed_actions: Any
    citations: Any
    confidence: float | None
    created_at: str


ToolClass = Literal["read", "propose", "act"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[BaseModel]
    output: type[BaseModel]  # every result is checked against it at the gateway
    cls: ToolClass = "read"  # read, propose or act: set here, never trusted from a hint
    scopes: tuple[str, ...] = ()  # the tenant data it reads
    untrusted: bool = False  # its result can carry text a customer or a model wrote
    max_calls: int = 8  # per run
    timeout_seconds: float = 10.0
    version: str = "1"

    def schema(self) -> dict[str, Any]:
        """The tool as the model sees it."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.args.model_json_schema(),
        }

    def contract(self) -> dict[str, Any]:
        """The whole declaration: what the bundle hash and the REST surface are built from."""
        return {
            **self.schema(),
            "output_schema": self.output.model_json_schema(),
            "class": self.cls,
            "scopes": list(self.scopes),
            "untrusted": self.untrusted,
            "max_calls": self.max_calls,
            "timeout_seconds": self.timeout_seconds,
            "version": self.version,
        }


_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "search_evidence",
        "Hybrid search over this tenant's evidence chunks of one type.",
        SearchEvidenceArgs,
        SearchEvidenceOut,
        scopes=("evidence",),
        untrusted=True,
        max_calls=6,
        timeout_seconds=20.0,
    ),
    ToolSpec(
        "get_chunk",
        "Full text and metadata of one chunk by id (this tenant only).",
        GetChunkArgs,
        ChunkOut,
        scopes=("evidence",),
        untrusted=True,
        max_calls=12,
    ),
    ToolSpec(
        "list_evidence",
        "Evidence rows of one type: id, title, issued_at, subject; newest first.",
        ListEvidenceArgs,
        ListEvidenceOut,
        scopes=("evidence",),
        untrusted=True,
    ),
    ToolSpec(
        "get_evidence_chunks",
        "Every chunk of one evidence row, in order, with page numbers.",
        GetEvidenceChunksArgs,
        ChunksOut,
        scopes=("evidence",),
        untrusted=True,
    ),
    ToolSpec(
        "find_exact",
        "Exact matches in evidence text: a literal string, or every CVE, ARN, email, date or"
        " control id. Each match has its document version, page, offsets and chunk.",
        FindExactArgs,
        FindExactOut,
        scopes=("evidence",),
        untrusted=True,
    ),
    ToolSpec(
        "outline",
        "One document version's structure: headings and tables with page and offsets, and"
        " its chunks' spans. Read a section next with read_span.",
        OutlineArgs,
        OutlineOut,
        scopes=("evidence",),
        untrusted=True,
        max_calls=6,
    ),
    ToolSpec(
        "read_span",
        "The exact text of one document version between two offsets (at most"
        f" {documents.MAX_SPAN} characters), with the chunks it falls in.",
        ReadSpanArgs,
        ReadSpanOut,
        scopes=("evidence",),
        untrusted=True,
        max_calls=12,
    ),
    ToolSpec(
        "find_access",
        "Typed facts: who holds which access, joined over people and grants, e.g. leavers"
        " who still hold admin on github: assets. Truncation is explicit.",
        FindAccessArgs,
        FindAccessOut,
        scopes=("people", "access"),
        untrusted=True,  # names come from the customer's systems
        max_calls=4,
    ),
    ToolSpec(
        "list_assets",
        "This tenant's assets: id, name, type, tags; optionally filtered by tag.",
        ListAssetsArgs,
        ListAssetsOut,
        scopes=("inventory",),
    ),
    ToolSpec(
        "get_person",
        "Merged view of one person: names, emails, handles, status, linked accounts.",
        GetPersonArgs,
        PersonOut,
        scopes=("people",),
        untrusted=True,  # names and handles come from the customer's systems
    ),
    ToolSpec(
        "get_current_state",
        "Current compliance state of one (test, subject).",
        GetCurrentStateArgs,
        StateOut,
        scopes=("state",),
    ),
    ToolSpec(
        "get_verdict",
        "One verdict: status, reasoning, verified citations, chunk ids, tier, model.",
        GetVerdictArgs,
        VerdictOut,
        scopes=("verdicts",),
        untrusted=True,  # reasoning and quotes were written from evidence
    ),
    ToolSpec(
        "list_findings",
        "Current compliance-state rows, optionally by status or test.",
        ListFindingsArgs,
        FindingsOut,
        scopes=("state",),
    ),
    ToolSpec(
        "get_access_grants",
        "Latest observation of every grant held by one canonical person.",
        GetAccessGrantsArgs,
        GrantsOut,
        scopes=("people", "access"),
    ),
    ToolSpec(
        "get_control",
        "A control's text, its requirements under both frameworks, its tests.",
        GetControlArgs,
        ControlOut,
    ),
    ToolSpec(
        "get_framework_rollup",
        "Per-requirement state counts and failing subjects for one framework.",
        GetFrameworkRollupArgs,
        RollupOut,
        scopes=("state",),
    ),
    ToolSpec(
        "get_investigation",
        "The investigation recorded for one verdict, if any.",
        GetInvestigationArgs,
        InvestigationOut,
        scopes=("verdicts",),
        untrusted=True,
    ),
)
TOOL_SPECS: dict[str, ToolSpec] = {s.name: s for s in _SPECS}


@dataclass
class ToolContext:
    """What a bound tool may see; the tenant is inside `db` and nowhere else."""

    db: ScopedDB
    embedder: EmbeddingClient
    reranker: Reranker | None
    retrieval: RetrievalConfig


ToolFn = Callable[[ToolContext, BaseModel], Any]


def missing(what: str, detail: str) -> dict[str, Any]:
    """Data the tool needs is absent: an error that names it, never an empty list (A-112)."""
    return {"error": detail, "missing": what}


def _redacted_row(row: dict[str, Any]) -> dict[str, Any]:
    """Chunk text never leaves a tool unredacted: S6 applies to tool results as it does
    to the evidence fence, so a secret cannot reach a model through a tool loop."""
    text, hits = redact(str(row.get("text") or ""))
    return {**row, "text": text, "redactions": hits}


def _redaction_count(result: Any) -> int:
    if not isinstance(result, dict):
        return 0
    own = result.get("redactions")
    total = len(own) if isinstance(own, list) else 0
    for chunk in [*(result.get("chunks") or []), *(result.get("matches") or [])]:
        if isinstance(chunk, dict) and isinstance(chunk.get("redactions"), list):
            total += len(chunk["redactions"])
    return total


def _chunk_dict(c: hybrid.RetrievedChunk) -> dict[str, Any]:
    return _redacted_row(
        {
            "chunk_id": c.chunk_id,
            "evidence_entity_id": c.evidence_entity_id,
            "evidence_type": c.evidence_type,
            "subject_entity_id": c.subject_entity_id,
            "page": c.page,
            "source_ref": c.source_ref,
            "text": c.text,
            "scores": c.scores(),
            "start_offset": c.start_offset,
            "end_offset": c.end_offset,
        }
    )


def _holds_chunks(ctx: ToolContext, evidence_type: str) -> bool:
    row = ctx.db.fetch_one(
        "SELECT 1 FROM chunks WHERE evidence_type = %s AND NOT is_distractor LIMIT 1",
        (evidence_type,),
    )
    return row is not None


def _issued_between(ctx: ToolContext, a: SearchEvidenceArgs) -> list[str] | None:
    if a.issued_after is None and a.issued_before is None:
        return None
    return [
        e.entity_id
        for e in repository.current_evidence(ctx.db, a.evidence_type)
        if (a.issued_after is None or (e.issued_at or e.observed_at).date() >= a.issued_after)
        and (a.issued_before is None or (e.issued_at or e.observed_at).date() <= a.issued_before)
    ]


def search_evidence(ctx: ToolContext, args: BaseModel) -> Any:
    a = SearchEvidenceArgs.model_validate(args.model_dump())
    within = _issued_between(ctx, a)
    if within is not None and not within:
        return missing(
            a.evidence_type.value,
            f"no {a.evidence_type.value} evidence was issued in that period",
        )
    as_of = a.as_of.replace(tzinfo=UTC) if a.as_of and a.as_of.tzinfo is None else a.as_of
    result = hybrid.retrieve(
        ctx.db,
        ctx.embedder,
        a.query,
        evidence_type=a.evidence_type.value,
        subject_entity_id=a.subject_entity_id,
        reranker=ctx.reranker,
        config=ctx.retrieval,
        top_k=a.top_k,
        as_of=as_of,
        evidence_ids=within,
    )
    if not result.chunks and not _holds_chunks(ctx, a.evidence_type.value):
        return missing(a.evidence_type.value, f"no {a.evidence_type.value} evidence is held")
    return {
        "chunks": [_chunk_dict(c) for c in result.chunks],
        "events": result.events,
        "embed_model": result.embed_model,  # which calibration its scores belong to
        "rerank_model": result.rerank_model,
    }


def get_chunk(ctx: ToolContext, args: BaseModel) -> Any:
    a = GetChunkArgs.model_validate(args.model_dump())
    row = ctx.db.fetch_one(
        "SELECT chunk_id, evidence_entity_id, evidence_type, subject_entity_id, page, source_ref,"
        " text, start_offset, end_offset FROM chunks WHERE chunk_id = %s AND NOT is_distractor",
        (a.chunk_id,),
    )
    if row is None:  # cited before a re-index or a generation switch: read the version
        old = generations.chunk_from_versions(ctx.db, [a.chunk_id]).get(a.chunk_id)
        if old is None:
            return {"error": "not found"}
        row = tuple(old[k] for k in _CHUNK_KEYS)
    return _redacted_row(dict(zip(_CHUNK_KEYS, row, strict=True)))


_CHUNK_KEYS = (
    "chunk_id",
    "evidence_entity_id",
    "evidence_type",
    "subject_entity_id",
    "page",
    "source_ref",
    "text",
    "start_offset",
    "end_offset",
)


def list_evidence(ctx: ToolContext, args: BaseModel) -> Any:
    a = ListEvidenceArgs.model_validate(args.model_dump())
    rows = repository.current_evidence(ctx.db, a.evidence_type)
    if not rows:
        return missing(a.evidence_type.value, f"no {a.evidence_type.value} evidence is held")
    ordered = sorted(rows, key=lambda e: (e.issued_at or e.observed_at, e.entity_id), reverse=True)
    return {
        "evidence": [
            {
                "evidence_entity_id": e.entity_id,
                "title": e.title,
                "issued_at": (e.issued_at or e.observed_at).isoformat(),
                "subject_entity_id": e.subject_entity_id,
                "source_ref": e.raw_ref,
            }
            for e in ordered
        ]
    }


def get_evidence_chunks(ctx: ToolContext, args: BaseModel) -> Any:
    a = GetEvidenceChunksArgs.model_validate(args.model_dump())
    rows = ctx.db.fetch_all(
        "SELECT chunk_id, evidence_entity_id, evidence_type, subject_entity_id, page, source_ref,"
        " text, start_offset, end_offset FROM chunks WHERE evidence_entity_id = %s"
        " AND NOT is_distractor ORDER BY ordinal",
        (a.evidence_entity_id,),
    )
    if not rows:
        return missing("chunks", f"no chunks are held for {a.evidence_entity_id}")
    return {"chunks": [_redacted_row(dict(zip(_CHUNK_KEYS, r, strict=True))) for r in rows]}


def _span_refs(drafts: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": d.chunk_id,
            "page": d.page,
            "start_offset": d.start_offset,
            "end_offset": d.end_offset,
        }
        for d in drafts
    ]


def _version(
    ctx: ToolContext, evidence_type: EvidenceType, entity: str, version: str | None
) -> Any:
    found = repository.evidence_version(ctx.db, entity, version)
    if found is None or found.evidence_type != evidence_type:
        return None  # another type is not this tool's to read (the Policy Agent's pin)
    return found


def find_exact(ctx: ToolContext, args: BaseModel) -> Any:
    a = FindExactArgs.model_validate(args.model_dump())
    held = repository.current_evidence(ctx.db, a.evidence_type)
    if a.evidence_entity_id is not None:
        held = [e for e in held if e.entity_id == a.evidence_entity_id]
    if not held:
        what = a.evidence_type.value if a.evidence_type else "evidence"
        return missing(what, f"no {what} is held to search")
    matches, total = documents.find(held, text=a.text, kind=a.kind, limit=a.limit)
    by_id = {e.entity_id: e for e in held}
    out = []
    for m in matches:
        inside = documents.covering(by_id[m.evidence_entity_id], m.page, m.start, m.end)
        out.append(
            _redacted_row(
                {
                    "evidence_entity_id": m.evidence_entity_id,
                    "version": m.version,
                    "evidence_type": m.evidence_type,
                    "page": m.page,
                    "start_offset": m.start,
                    "end_offset": m.end,
                    "match": m.match,
                    "text": m.snippet,
                    "chunk_id": inside[0].chunk_id if inside else None,
                }
            )
        )
    return {"matches": out, "total": total, "truncated": total > len(out)}


def outline(ctx: ToolContext, args: BaseModel) -> Any:
    a = OutlineArgs.model_validate(args.model_dump())
    found = _version(ctx, a.evidence_type, a.evidence_entity_id, a.version)
    if found is None:
        return {"error": "not found"}
    return {
        "evidence_entity_id": found.entity_id,
        "version": found.source_event_id,
        "evidence_type": found.evidence_type.value,
        "title": redact(found.title)[0],
        "observed_at": found.observed_at.isoformat(),
        "pages": len(found.page_texts) if found.page_texts is not None else None,
        "length": sum(len(body) for _, body in documents.pages(found)),
        "entries": [
            {
                "kind": e.kind,
                "level": e.level,
                "title": redact(e.title)[0],
                "page": e.page,
                "start_offset": e.start,
                "end_offset": e.end,
            }
            for e in documents.outline(found)
        ],
        "chunks": _span_refs(documents.chunks_of(found)),
    }


def read_span(ctx: ToolContext, args: BaseModel) -> Any:
    a = ReadSpanArgs.model_validate(args.model_dump())
    found = _version(ctx, a.evidence_type, a.evidence_entity_id, a.version)
    if found is None:
        return {"error": "not found"}
    span = documents.read(found, a.start, a.end, a.page)
    if span is None:
        pages = len(found.page_texts) if found.page_texts is not None else None
        return {"error": f"no page {a.page}: the document has {pages or 'no'} pages"}
    return _redacted_row(
        {
            "evidence_entity_id": found.entity_id,
            "version": found.source_event_id,
            "evidence_type": found.evidence_type.value,
            "page": span.page,
            "start_offset": span.start,
            "end_offset": span.end,
            "truncated": span.truncated,
            "text": span.text,
            "chunks": _span_refs(documents.covering(found, span.page, span.start, span.end)),
        }
    )


def find_access(ctx: ToolContext, args: BaseModel) -> Any:
    """A join inside a typed tool (who holds what), not a graph built from documents."""
    a = FindAccessArgs.model_validate(args.model_dump())
    grants = repository.current_grants(ctx.db)
    if not grants:
        return missing("grants", "no access grants are held: no access source has been pulled")
    links = resolver.canonical_map(ctx.db)
    people: dict[str, list[Any]] = {}
    for person in repository.current_persons(ctx.db):
        people.setdefault(links.get(person.entity_id, person.entity_id), []).append(person)

    def status(members: list[Any]) -> str:
        held = {m.employment_status for m in members}
        for value in (EmploymentStatus.terminated, EmploymentStatus.active):
            if value in held:
                return value.value
        return EmploymentStatus.unknown.value

    rows: list[dict[str, Any]] = []
    for g in sorted(grants, key=lambda x: (x.person_entity_id, x.asset_entity_id, x.role)):
        canonical = links.get(g.person_entity_id, g.person_entity_id)
        members = people.get(canonical, [])
        held_status = status(members)
        if g.state != a.state:
            continue
        if a.employment_status is not None and held_status != a.employment_status.value:
            continue
        if a.asset_prefix is not None and not g.asset_entity_id.startswith(a.asset_prefix):
            continue
        if a.role is not None and g.role.lower() != a.role.lower():
            continue
        rows.append(
            {
                "canonical_person_id": canonical,
                "display_name": next((m.display_name for m in members if m.display_name), None),
                "employment_status": held_status,
                "account_entity_id": g.person_entity_id,
                "asset_entity_id": g.asset_entity_id,
                "role": g.role,
                "state": g.state.value,
                "state_effective_at": g.state_effective_at.isoformat(),
                "observed_at": g.observed_at.isoformat(),
            }
        )
    return {
        "rows": rows[: a.limit],
        "total": len(rows),
        "truncated": len(rows) > a.limit,
        "read_at": datetime.now(UTC).isoformat(),
    }


def list_assets(ctx: ToolContext, args: BaseModel) -> Any:
    a = ListAssetsArgs.model_validate(args.model_dump())
    assets = repository.current_assets(ctx.db)
    if not assets:
        return missing("assets", "no assets are held: no inventory has been pulled")
    return {
        "assets": [
            {
                "entity_id": x.entity_id,
                "name": x.name,
                "asset_type": x.asset_type,
                "tags": list(x.tags),
            }
            for x in assets
            if a.tag is None or a.tag in x.tags
        ]
    }


def get_person(ctx: ToolContext, args: BaseModel) -> Any:
    a = GetPersonArgs.model_validate(args.model_dump())
    links = resolver.canonical_map(ctx.db)
    members = {pid for pid, canonical in links.items() if canonical == a.canonical_person_id}
    members.add(a.canonical_person_id)
    persons = [p for p in repository.current_persons(ctx.db) if p.entity_id in members]
    if not persons:
        return {"error": "not found"}
    return {
        "canonical_person_id": a.canonical_person_id,
        "accounts": [
            {
                "entity_id": p.entity_id,
                "display_name": p.display_name,
                "emails": list(p.emails),
                "handles": p.handles,
                "employment_status": p.employment_status.value,
                "account_kind": p.account_kind.value,
            }
            for p in persons
        ],
    }


def get_current_state(ctx: ToolContext, args: BaseModel) -> Any:
    a = GetCurrentStateArgs.model_validate(args.model_dump())
    subject_key = a.subject_key if "|" in a.subject_key else f"{a.test_id}|{a.subject_key}"
    row = bitemporal.current_state(ctx.db, subject_key)
    if row is None:
        return missing("evaluation", f"no evaluation of {subject_key} yet")
    held = stored_exceptions.active(ctx.db).get(subject_key)
    until = held.valid_until if held is not None and row.status.value == "fail" else None
    return {
        "status": row.status.value,
        "shown_as": stored_exceptions.label(row.status.value, until),
        "exception": (
            {"until": until.isoformat(), "owner": held.owner, "reason": held.reason}
            if held is not None and until is not None
            else None
        ),
        "valid_from": row.valid_from.isoformat(),
        "verdict_id": row.verdict_id,
        "reason": row.reason,
    }


def get_verdict(ctx: ToolContext, args: BaseModel) -> Any:
    from ccp.evaluators import verdict as verdicts

    a = GetVerdictArgs.model_validate(args.model_dump())
    v = verdicts.load(ctx.db, a.verdict_id)
    if v is None:
        return {"error": "not found"}
    return {
        "verdict_id": v.verdict_id,
        "test_id": v.test_id,
        "subject_key": v.subject_key,
        "status": v.status.value,
        "reasoning": v.reasoning,
        "confidence": v.confidence,
        "tier": v.tier,
        "model": v.model,
        "citations": [c.model_dump() for c in v.citations if c.verified],
        "retrieved_chunk_ids": list(v.retrieved_chunk_ids),
        "evidence_refs": list(v.evidence_refs),
    }


def list_findings(ctx: ToolContext, args: BaseModel) -> Any:
    a = ListFindingsArgs.model_validate(args.model_dump())
    rows = bitemporal.heads(ctx.db)
    if not rows:
        return missing("evaluations", "no evaluations yet: nothing has been assessed")
    held = stored_exceptions.excepted_until(ctx.db)
    return {
        "findings": [
            {
                "subject_key": r.subject_key,
                "test_id": r.test_id,
                "status": r.status.value,
                "shown_as": stored_exceptions.label(r.status.value, held.get(r.subject_key)),
                "verdict_id": r.verdict_id,
            }
            for r in rows
            if (a.status is None or r.status.value == a.status)
            and (a.test_id is None or r.test_id == a.test_id)
        ]
    }


def get_access_grants(ctx: ToolContext, args: BaseModel) -> Any:
    a = GetAccessGrantsArgs.model_validate(args.model_dump())
    links = resolver.canonical_map(ctx.db)
    members = {pid for pid, canonical in links.items() if canonical == a.canonical_person_id}
    members.add(a.canonical_person_id)
    grants = repository.current_grants(ctx.db)
    if not grants:
        return missing("grants", "no access grants are held: no access source has been pulled")
    if not any(p.entity_id in members for p in repository.current_persons(ctx.db)):
        return {"error": "not found"}
    return {
        "grants": [
            {
                "entity_id": g.entity_id,
                "asset_entity_id": g.asset_entity_id,
                "role": g.role,
                "state": g.state.value,
                "state_effective_at": g.state_effective_at.isoformat(),
                "connector": g.source_connector,
            }
            for g in grants
            if g.person_entity_id in members
        ]
    }


def get_control(ctx: ToolContext, args: BaseModel) -> Any:
    a = GetControlArgs.model_validate(args.model_dump())
    graph = knowledge.load()
    control = graph.controls.get(a.control_id)
    if control is None:
        return {"error": "not found"}
    requirements = []
    for ref in control.requirements:
        fw_id, _, req_id = ref.partition(":")
        req = graph.frameworks[fw_id].requirement(req_id)
        requirements.append(
            {"framework": fw_id, "requirement_id": req_id, "title": req.title, "intent": req.intent}
        )
    return {
        "control_id": control.control_id,
        "title": control.title,
        "description": control.description,
        "requirements": requirements,
        "tests": list(control.tests),
    }


def get_framework_rollup(ctx: ToolContext, args: BaseModel) -> Any:
    a = GetFrameworkRollupArgs.model_validate(args.model_dump())
    graph = knowledge.load()
    if a.framework_id not in graph.frameworks:
        return {"error": "not found"}
    covered = inventory.coverage(ctx.db)  # the same population the operator sees (A-124)
    held = stored_exceptions.excepted_until(ctx.db)  # "fail (excepted until D)" (A-132)
    result = knowledge_rollup.rollup(graph, bitemporal.heads(ctx.db), a.framework_id, covered, held)
    return {
        "framework_id": a.framework_id,
        "no_evaluations_yet": result.no_evaluations_yet,
        "requirements": [
            {
                "requirement_id": r.requirement_id,
                "state": r.state,
                "shown_as": r.shown,
                "blocking_test_id": r.blocking_test_id,
                "blocking_subject_key": r.blocking_subject_key,
                "counts": r.counts,
                "pending_reasons": r.pending_reasons,
            }
            for r in result.requirements
        ],
    }


def get_investigation(ctx: ToolContext, args: BaseModel) -> Any:
    a = GetInvestigationArgs.model_validate(args.model_dump())
    row = ctx.db.fetch_one(
        "SELECT investigation_id, root_cause, root_cause_category, missing_evidence,"
        " proposed_actions, citations, confidence, created_at FROM investigations"
        " WHERE verdict_id = %s ORDER BY created_at DESC LIMIT 1",
        (a.verdict_id,),
    )
    if row is None:
        return {"error": "not found", "verdict_id": a.verdict_id}

    def load(value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else value

    return {
        "verdict_id": a.verdict_id,
        "investigation_id": row[0],
        "root_cause": row[1],
        "root_cause_category": row[2],
        "missing_evidence": load(row[3]),
        "proposed_actions": load(row[4]),
        "citations": load(row[5]),
        "confidence": row[6],
        "created_at": row[7].isoformat(),
    }


TOOL_FNS: dict[str, ToolFn] = {
    "search_evidence": search_evidence,
    "get_chunk": get_chunk,
    "list_evidence": list_evidence,
    "get_evidence_chunks": get_evidence_chunks,
    "find_exact": find_exact,
    "outline": outline,
    "read_span": read_span,
    "find_access": find_access,
    "list_assets": list_assets,
    "get_person": get_person,
    "get_current_state": get_current_state,
    "get_verdict": get_verdict,
    "list_findings": list_findings,
    "get_access_grants": get_access_grants,
    "get_control": get_control,
    "get_framework_rollup": get_framework_rollup,
    "get_investigation": get_investigation,
}


@dataclass(frozen=True)
class BoundTool:
    spec: ToolSpec
    _fn: ToolFn
    _ctx: ToolContext
    fixed_args: dict[str, Any] | None = None  # a graph may pin an argument (policy_document)
    gate: Gate | None = None  # policy.check and its log (A-113); none: every call is refused
    _calls: list[int] = field(default_factory=lambda: [0])  # this run's calls

    def _refuse(self, args: dict[str, Any], reason: str, **extra: Any) -> dict[str, Any]:
        """A call that never runs still gets a logged decision: a deny."""
        if self.gate is not None:
            self.gate.record(self.spec.name, self.spec.cls, args, Decision("deny", None, reason))
        return {"is_error": True, "error": reason, **extra}

    def call(self, raw_args: dict[str, Any]) -> dict[str, Any]:
        """The gateway (A-112, A-113): the per-run call limit, the arguments validated (extra
        fields forbidden), `policy.check` decided and logged, the run, and the result checked
        against the tool's output schema."""
        if self._calls[0] >= self.spec.max_calls:
            return self._refuse(
                raw_args,
                f"{self.spec.name} may be called at most {self.spec.max_calls} times in a run",
                rate_limited=True,
            )
        self._calls[0] += 1
        merged = dict(raw_args)
        for key, value in (self.fixed_args or {}).items():
            if key in merged and merged[key] != value:
                return self._refuse(
                    merged,
                    f"argument {key} is fixed to {value!r} in this graph",
                    fixed_argument=key,
                )
            merged[key] = value
        try:
            args = self.spec.args.model_validate(merged)
        except ValidationError as exc:
            return self._refuse(merged, f"invalid arguments: {exc.errors()[0].get('msg', exc)}")
        if self.gate is None:
            return {"is_error": True, "error": "not permitted: no policy gate", "denied": "none"}
        if self.spec.cls == "act" and self.gate.tainted is not None:  # the Rule of Two (A-114)
            return self._refuse(
                args.model_dump(mode="json"),
                f"not permitted: act tools are off once a run has read untrusted content"
                f" ({self.gate.tainted})",
                denied="taint",
                tainted=True,
            )
        decision = self.gate.decide(self.spec.name, self.spec.cls, args.model_dump(mode="json"))
        if not decision.permitted:
            return {
                "is_error": True,
                "error": f"not permitted: {decision.reason}",
                "denied": decision.rule_id or "default",
                "policy": decision.effect,
            }
        started = time.monotonic()
        result = self._fn(self._ctx, args)
        elapsed = time.monotonic() - started
        if isinstance(result, dict) and result.get("error"):
            return {"is_error": True, **result}
        try:
            self.spec.output.model_validate(result)
        except ValidationError:  # a tool bug: the model never sees a malformed result
            return {
                "is_error": True,
                "error": f"{self.spec.name} returned a malformed result",
                "malformed_result": True,
            }
        wrapped: dict[str, Any] = {"is_error": False, "result": result}
        if self.spec.untrusted:  # this run has now read text a customer or a model wrote
            self.gate.taint(f"{self.spec.name} returned untrusted content")
        if elapsed > self.spec.timeout_seconds:
            wrapped["timeout_exceeded"] = round(elapsed, 3)
        redactions = _redaction_count(result)
        if redactions:
            wrapped["redactions"] = redactions
        return wrapped


def bind_tools(
    ctx: ToolContext,
    names: tuple[str, ...],
    fixed: dict[str, dict[str, Any]] | None = None,
    *,
    gate: Gate,
) -> dict[str, BoundTool]:
    """The tools a run may use, each behind the gateway. A tool `policy.check` could never
    permit this principal leaves the list (A-113)."""
    unknown = sorted(set(names) - set(TOOL_SPECS))
    if unknown:
        raise KeyError(f"unknown tools {unknown}")
    permitted = set(gate.offered(names))
    return {
        name: BoundTool(TOOL_SPECS[name], TOOL_FNS[name], ctx, (fixed or {}).get(name), gate)
        for name in names
        if name in permitted
    }
