"""The Policy Agent graph (spec.md Layer 15 "Graph"), a declaration over the shared core.

    START -> load_control      pure: get_control (both frameworks' requirement text)
          -> gather            tool loop on the policy allowlist; search_evidence is fixed to
                               policy_document, so the model cannot read anything else
          -> analyze           one structured call -> GapReportDraft
          -> verify_citations  pure: a covered requirement keeps its status only if a citation
                               survives verification
          -> [draft mode] compose   plain-text call -> Markdown draft
          -> [draft mode] scrub     pure: secrets patterns (S6), other tenants' tokens (S9)
          -> finalize -> END

The graph has no write tool. The runner writes the draft file and opens the approval.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from langgraph.graph import END, START

from ccp.agent import core
from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.core import ConditionalEdge, GraphSpec, ModelDeps, NodePolicies, NodeSpec
from ccp.agent.state import AgentState
from ccp.agent.tools import BoundTool
from ccp.evaluators import citations, confidence
from ccp.evaluators.extraction import SchemaMeta
from ccp.evaluators.extraction.policy import (
    COMPOSE_PROMPT_VERSION,
    GAP_PROMPT_VERSION,
    GATHER_PROMPT_VERSION,
    SCHEMA_VERSION,
    GapReportDraft,
)
from ccp.llm.client import SystemBlock
from ccp.llm.prompts import load_prompt
from ccp.security import prompt_guard
from ccp.security.prompt_guard import FramedChunk
from ccp.security.secrets_patterns import redact
from ccp.tenancy import TenantContext

GRAPH_NAME = "policy"
TRAJECTORY_GAP = ("load_control", "gather", "analyze", "verify_citations", "finalize")
TRAJECTORY_DRAFT = (
    "load_control",
    "gather",
    "analyze",
    "verify_citations",
    "compose",
    "scrub",
    "finalize",
)
DRAFT_META = SchemaMeta(
    name="GapReportDraft",
    model=GapReportDraft,
    version=SCHEMA_VERSION,
    required=("gaps",),
    cited=(),
    value_in_quote=(),
)


def _on_tool_result(state: AgentState, tool: str, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("is_error"):
        return {}
    chunks: dict[str, dict[str, Any]] = {}
    payload = result.get("result", {})
    for c in payload.get("chunks", []) if isinstance(payload, dict) else []:
        if c.get("evidence_type") == "policy_document":  # belt and braces: the tool is fixed
            chunks[str(c["chunk_id"])] = dict(c)
    return {"retrieved_chunks": chunks, "evidence_coverage": {"policy_document": bool(chunks)}}


def token_ref(token: str) -> str:
    """A short stable reference to a forbidden token, so an event can say which token was
    removed without carrying the other tenant's name into the log or the terminal."""
    return hashlib.sha256(token.lower().encode("utf-8")).hexdigest()[:8]


def scrub_text(text: str, forbidden_tokens: list[str]) -> tuple[str, list[str]]:
    """Secrets patterns first (S6), then other tenants' names (S9), whole words only, any
    case. Returns the text and events that reference the removed token by hash."""
    cleaned, hits = redact(text)
    events = [f"draft_secret_redacted:{h}" for h in hits]
    for token in sorted({t for t in forbidden_tokens if t}, key=len, reverse=True):
        pattern = re.compile(rf"(?<![\w-]){re.escape(token)}(?![\w-])", re.IGNORECASE)
        cleaned, count = pattern.subn("[REDACTED:other_tenant]", cleaned)
        if count:
            events.append(f"draft_other_tenant_redacted:{token_ref(token)}:{count}")
    return cleaned, events


def build_spec(
    tenant: TenantContext,
    tools: dict[str, BoundTool],
    deps: ModelDeps,
    *,
    now: datetime,
    forbidden_tokens: list[str],
) -> GraphSpec:
    allowed = ALLOWLISTS[GRAPH_NAME]
    policies = NodePolicies.default()

    def load_control(state: AgentState) -> dict[str, Any]:
        control_id = str(state.get("subject_key"))
        control = tools["get_control"].call({"control_id": control_id})
        log = [
            {
                "node": "load_control",
                "tool": "get_control",
                "arguments": {"control_id": control_id},
                "is_error": bool(control.get("is_error")),
            }
        ]
        context = {"control": control.get("result") or {"error": control.get("error")}}
        return {"context": context, "tool_log": log}

    def goal(state: AgentState) -> str:
        control = (state.get("context") or {}).get("control") or {}
        return (
            f"Gap analysis for control {control.get('control_id')}: {control.get('title')}.\n"
            f"Requirements: {json.dumps(control.get('requirements'), default=str)[:4000]}\n"
            "Find the policy text that addresses each requirement."
        )

    gather = core.make_tool_loop(
        "gather",
        deps=deps,
        tools=tools,
        allowed=allowed,
        prompt_version=GATHER_PROMPT_VERSION,
        goal=goal,
        on_result=_on_tool_result,
    )

    def describe(state: AgentState) -> str:
        control = (state.get("context") or {}).get("control") or {}
        requirement_ids = [
            f"{r['framework']}:{r['requirement_id']}" for r in control.get("requirements", [])
        ]
        return (
            "Control under analysis (system data, not evidence):\n"
            + json.dumps(control, default=str)[:12000]
            + "\n\nRequirement ids to report on, exactly: "
            + json.dumps(requirement_ids)
        )

    analyze = core.make_structured_node(
        "analyze",
        deps=deps,
        prompt_version=GAP_PROMPT_VERSION,
        meta=DRAFT_META,
        select_chunks=lambda s: list(s.get("retrieved_chunks", {}).values()),
        describe=describe,
        require_chunks=False,
    )

    def verify(state: AgentState) -> dict[str, Any]:
        raw = (state.get("extracted_raw") or {}).get("analyze")
        fields = (raw or {}).get("fields") or {}
        chunks = state.get("retrieved_chunks", {})
        control = (state.get("context") or {}).get("control") or {}
        expected = [
            f"{r['framework']}:{r['requirement_id']}" for r in control.get("requirements", [])
        ]
        by_id = {str(g.get("requirement_id")): g for g in fields.get("gaps") or []}
        gaps: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        events: list[str] = []
        given = verified_count = 0
        for req_id in expected:
            gap = by_id.get(req_id) or {
                "requirement_id": req_id,
                "covered": False,
                "citations": [],
                "gap_description": "not analyzed",
            }
            if req_id not in by_id:
                events.append(f"gap_missing_from_analysis:{req_id}")
            covering: list[dict[str, Any]] = []
            for cited in gap.get("citations") or []:
                given += 1
                chunk_id = str(cited.get("chunk_id", ""))
                quote = str(cited.get("quote", ""))[: citations.MAX_QUOTE_CHARS]
                chunk = chunks.get(chunk_id)
                ok = chunk is not None and citations.quote_supported(
                    quote, str(chunk.get("text", ""))
                )
                record = {"chunk_id": chunk_id, "quote": quote, "supports": req_id, "verified": ok}
                records.append(record)
                if ok:
                    verified_count += 1
                    covering.append(record)
                else:
                    kind = "unknown_chunk" if chunk is None else "quote_not_found"
                    events.append(f"citation_{kind}:{req_id}:{chunk_id}")
            covered = bool(gap.get("covered")) and bool(covering)
            if gap.get("covered") and not covering:
                events.append(f"gap_uncovered_by_verification:{req_id}")
            gaps.append(
                {
                    "requirement_id": req_id,
                    "covered": covered,
                    "covering_citations": covering,
                    "gap_description": None
                    if covered
                    else (
                        gap.get("gap_description")
                        or "no verified policy text addresses this requirement"
                    ),
                }
            )
        stats = {
            "coverage": 1.0 if gaps else 0.0,
            "citation_rate": verified_count / given if given else 1.0,
        }
        return {
            "extracted": {"gaps": gaps, "summary": str(fields.get("summary") or "")},
            "verified_citations": records,
            "citation_stats": stats,
            "guardrail_events": events,
        }

    def compose(state: AgentState) -> dict[str, Any]:
        llm = deps.require()
        prompt = load_prompt(COMPOSE_PROMPT_VERSION)
        nonce = state.get("fence_nonce") or prompt_guard.new_nonce()
        chunks = list(state.get("retrieved_chunks", {}).values())
        framed = prompt_guard.frame_evidence(
            [
                FramedChunk(
                    str(c["chunk_id"]),
                    str(c["text"]),
                    str(c["evidence_type"]),
                    c.get("subject_entity_id"),
                    c.get("page"),
                    str(c.get("source_ref", "")),
                )
                for c in chunks
            ],
            nonce,
        )
        report = state.get("extracted") or {}
        control = (state.get("context") or {}).get("control") or {}
        control_json = json.dumps(control, default=str)[:6000]
        report_json = json.dumps(report, default=str)[:6000]
        user = (
            f"{prompt_guard.fence_declaration(nonce)}\n\nControl: {control_json}\n\n"
            f"Gap report: {report_json}\n\n{framed.text}\nWrite the Markdown now."
        )
        completion = llm.complete(
            system=[SystemBlock(prompt.text, cache=True)],
            user=user,
            model=deps.model,
            tracker=deps.tracker,
            max_tokens=2000,
        )
        return {
            "plan": completion.text,
            "requests": {
                "compose": {
                    "request": completion.request,
                    "response": completion.response,
                    "model": completion.model,
                    "prompt_version": COMPOSE_PROMPT_VERSION,
                    "usage": completion.usage.__dict__,
                }
            },
            "guardrail_events": list(framed.events),
            "fence_nonce": nonce,
        }

    def scrub(state: AgentState) -> dict[str, Any]:
        cleaned, events = scrub_text(str(state.get("plan") or ""), forbidden_tokens)
        return {"plan": cleaned, "guardrail_events": events}

    def finalize(state: AgentState) -> dict[str, Any]:
        report = dict(state.get("extracted") or {})
        stats = state.get("citation_stats") or {"coverage": 0.0, "citation_rate": 1.0}
        chunks = state.get("retrieved_chunks", {})
        injection = any(
            e.startswith("injection_marker_detected:") for e in state.get("guardrail_events", [])
        )
        conf = confidence.compute(
            coverage=float(stats["coverage"]),
            citation_rate=float(stats["citation_rate"]),
            retrieval_quality=1.0 if chunks else None,
            no_evidence=not chunks,
            injection=injection,
            unresolved_evidence_type=any(  # a gather that stopped early (A-100)
                e.startswith("tool_loop_stopped:") for e in state.get("guardrail_events", [])
            ),
        )
        gaps = list(report.get("gaps") or [])
        if not chunks:  # no policy_document chunks at all: every gap uncovered, capped 0.20
            gaps = [
                {
                    **g,
                    "covered": False,
                    "covering_citations": [],
                    "gap_description": g.get("gap_description") or "no policy text found",
                }
                for g in gaps
            ]
        requests = state.get("requests", {})
        final = {
            "control_id": state.get("subject_key"),
            "gaps": gaps,
            "summary": str(report.get("summary") or ""),
            "confidence": conf.value,
            "confidence_cap": conf.cap,
            "model": (requests.get("analyze") or {}).get("model"),
            "prompt_version": GAP_PROMPT_VERSION,
            "guardrail_events": list(state.get("guardrail_events", [])) + list(conf.events),
            "thread_id": state.get("thread_id"),
            "request_json": {
                "thread_id": state.get("thread_id"),
                "calls": requests,
                "tool_log": state.get("tool_log", []),
            },
            "response_json": {"analyze": (requests.get("analyze") or {}).get("response")},
            "markdown": state.get("plan") if state.get("mode") == "draft" else None,
            "retrieved_chunk_ids": sorted(chunks),
        }
        return {"final": final}

    def after_verify(state: AgentState) -> str:
        return "compose" if state.get("mode") == "draft" else "finalize"

    def node(name: str, fn: core.NodeFn) -> NodeSpec:
        return NodeSpec(
            name,
            core.with_policies(fn, name, tenant, deps.tracker, policies.timeout_seconds),
            policies,
        )

    return GraphSpec(
        name=GRAPH_NAME,
        tools=tuple(sorted(tools)),
        nodes=(
            node("load_control", load_control),
            node("gather", gather),
            node("analyze", analyze),
            node("verify_citations", verify),
            node("compose", compose),
            node("scrub", scrub),
            node("finalize", finalize),
        ),
        edges=(
            (START, "load_control"),
            ("load_control", "gather"),
            ("gather", "analyze"),
            ("analyze", "verify_citations"),
            ("compose", "scrub"),
            ("scrub", "finalize"),
            ("finalize", END),
        ),
        conditional_edges=(
            ConditionalEdge(
                "verify_citations", after_verify, {"compose": "compose", "finalize": "finalize"}
            ),
        ),
        output_schema=GapReportDraft,
    )
