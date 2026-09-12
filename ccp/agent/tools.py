"""Read-only tools for every graph (spec.md Layer 8 "Tools, all read-only, with scope").

Every tool takes its tenant from the closure created when the graph is built, never from
an argument the model can set: no tool schema has a `tenant_id` field and every schema
forbids extra fields, so a model passing one gets a validation error, not another tenant.
This module imports nothing with a side effect (tests/unit/test_layering.py); each tool
only reads through ScopedDB.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ccp.identity import resolver
from ccp.knowledge import graph as knowledge
from ccp.knowledge import rollup as knowledge_rollup
from ccp.normalize import repository
from ccp.retrieval import hybrid
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import EmbeddingClient
from ccp.retrieval.rerank import Reranker
from ccp.storage import bitemporal
from ccp.storage.scoped import ScopedDB
from ccp.types import EvidenceType

MAX_TOP_K = 8


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchEvidenceArgs(_Args):
    query: str = Field(min_length=1)
    evidence_type: EvidenceType
    top_k: int = Field(default=6, ge=1, le=MAX_TOP_K)


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


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[BaseModel]

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.args.model_json_schema(),
        }


_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "search_evidence",
        "Hybrid search over this tenant's evidence chunks of one type.",
        SearchEvidenceArgs,
    ),
    ToolSpec(
        "get_chunk",
        "Full text and metadata of one chunk by id (this tenant only).",
        GetChunkArgs,
    ),
    ToolSpec(
        "list_evidence",
        "Evidence rows of one type: id, title, issued_at, subject; newest first.",
        ListEvidenceArgs,
    ),
    ToolSpec(
        "get_evidence_chunks",
        "Every chunk of one evidence row, in order, with page numbers.",
        GetEvidenceChunksArgs,
    ),
    ToolSpec(
        "list_assets",
        "This tenant's assets: id, name, type, tags; optionally filtered by tag.",
        ListAssetsArgs,
    ),
    ToolSpec(
        "get_person",
        "Merged view of one person: names, emails, handles, status, linked accounts.",
        GetPersonArgs,
    ),
    ToolSpec(
        "get_current_state",
        "Current compliance state of one (test, subject).",
        GetCurrentStateArgs,
    ),
    ToolSpec(
        "get_verdict",
        "One verdict: status, reasoning, verified citations, chunk ids, tier, model.",
        GetVerdictArgs,
    ),
    ToolSpec(
        "list_findings",
        "Current compliance-state rows, optionally by status or test.",
        ListFindingsArgs,
    ),
    ToolSpec(
        "get_access_grants",
        "Latest observation of every grant held by one canonical person.",
        GetAccessGrantsArgs,
    ),
    ToolSpec(
        "get_control",
        "A control's text, its requirements under both frameworks, its tests.",
        GetControlArgs,
    ),
    ToolSpec(
        "get_framework_rollup",
        "Per-requirement state counts and failing subjects for one framework.",
        GetFrameworkRollupArgs,
    ),
    ToolSpec(
        "get_investigation",
        "The investigation recorded for one verdict, if any.",
        GetInvestigationArgs,
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


def _chunk_dict(c: hybrid.RetrievedChunk) -> dict[str, Any]:
    return {
        "chunk_id": c.chunk_id,
        "evidence_entity_id": c.evidence_entity_id,
        "evidence_type": c.evidence_type,
        "subject_entity_id": c.subject_entity_id,
        "page": c.page,
        "source_ref": c.source_ref,
        "text": c.text,
        "scores": c.scores(),
    }


def search_evidence(ctx: ToolContext, args: BaseModel) -> Any:
    a = SearchEvidenceArgs.model_validate(args.model_dump())
    result = hybrid.retrieve(
        ctx.db,
        ctx.embedder,
        a.query,
        evidence_type=a.evidence_type.value,
        reranker=ctx.reranker,
        config=ctx.retrieval,
        top_k=a.top_k,
    )
    return {"chunks": [_chunk_dict(c) for c in result.chunks], "events": result.events}


def get_chunk(ctx: ToolContext, args: BaseModel) -> Any:
    a = GetChunkArgs.model_validate(args.model_dump())
    row = ctx.db.fetch_one(
        "SELECT chunk_id, evidence_entity_id, evidence_type, subject_entity_id, page, source_ref,"
        " text FROM chunks WHERE chunk_id = %s AND NOT is_distractor",
        (a.chunk_id,),
    )
    if row is None:
        return {"error": "not found"}
    keys = (
        "chunk_id",
        "evidence_entity_id",
        "evidence_type",
        "subject_entity_id",
        "page",
        "source_ref",
        "text",
    )
    return dict(zip(keys, row, strict=True))


def list_evidence(ctx: ToolContext, args: BaseModel) -> Any:
    a = ListEvidenceArgs.model_validate(args.model_dump())
    rows = repository.current_evidence(ctx.db, a.evidence_type)
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
        " text FROM chunks WHERE evidence_entity_id = %s AND NOT is_distractor ORDER BY ordinal",
        (a.evidence_entity_id,),
    )
    keys = (
        "chunk_id",
        "evidence_entity_id",
        "evidence_type",
        "subject_entity_id",
        "page",
        "source_ref",
        "text",
    )
    return {"chunks": [dict(zip(keys, r, strict=True)) for r in rows]}


def list_assets(ctx: ToolContext, args: BaseModel) -> Any:
    a = ListAssetsArgs.model_validate(args.model_dump())
    assets = repository.current_assets(ctx.db)
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
        return {"state": "no_evaluations_yet"}
    return {
        "status": row.status.value,
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
    return {
        "findings": [
            {
                "subject_key": r.subject_key,
                "test_id": r.test_id,
                "status": r.status.value,
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
            for g in repository.current_grants(ctx.db)
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
    result = knowledge_rollup.rollup(graph, bitemporal.heads(ctx.db), a.framework_id)
    return {
        "framework_id": a.framework_id,
        "no_evaluations_yet": result.no_evaluations_yet,
        "requirements": [
            {
                "requirement_id": r.requirement_id,
                "state": r.state,
                "blocking_test_id": r.blocking_test_id,
                "blocking_subject_key": r.blocking_subject_key,
                "counts": r.counts,
            }
            for r in result.requirements
        ],
    }


def get_investigation(ctx: ToolContext, args: BaseModel) -> Any:
    GetInvestigationArgs.model_validate(args.model_dump())
    return {"error": "not found", "note": "investigations arrive in Phase 12"}


TOOL_FNS: dict[str, ToolFn] = {
    "search_evidence": search_evidence,
    "get_chunk": get_chunk,
    "list_evidence": list_evidence,
    "get_evidence_chunks": get_evidence_chunks,
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

    def call(self, raw_args: dict[str, Any]) -> dict[str, Any]:
        """Validate the model's arguments (extra fields forbidden), run, wrap the result."""
        merged = dict(raw_args)
        for key, value in (self.fixed_args or {}).items():
            if key in merged and merged[key] != value:
                return {
                    "is_error": True,
                    "error": f"argument {key} is fixed to {value!r} in this graph",
                    "fixed_argument": key,
                }
            merged[key] = value
        try:
            args = self.spec.args.model_validate(merged)
        except ValidationError as exc:
            return {
                "is_error": True,
                "error": f"invalid arguments: {exc.errors()[0].get('msg', exc)}",
            }
        result = self._fn(self._ctx, args)
        if isinstance(result, dict) and result.get("error"):
            return {"is_error": True, **result}
        return {"is_error": False, "result": result}


def bind_tools(
    ctx: ToolContext,
    names: tuple[str, ...],
    fixed: dict[str, dict[str, Any]] | None = None,
) -> dict[str, BoundTool]:
    unknown = sorted(set(names) - set(TOOL_SPECS))
    if unknown:
        raise KeyError(f"unknown tools {unknown}")
    return {
        name: BoundTool(TOOL_SPECS[name], TOOL_FNS[name], ctx, (fixed or {}).get(name))
        for name in names
    }
