"""The Investigator graph (spec.md Layer 14 "Graph"), a declaration over the shared core.

    START -> load_context   pure tool calls: get_verdict, get_current_state, the control text,
                            the subject's grants
          -> gather         the bounded tool loop on the investigator allowlist (<= 8 calls)
          -> diagnose       one structured call -> InvestigationDraft
          -> verify_citations   pure: every citation against the gathered chunks
          -> bound_actions  pure: drop anything outside the catalogue or without its target
                            fields; fill reversible and live_capable from the catalogue
          -> finalize -> END

No interrupt: the runner persists the Investigation and opens the approval items.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from langgraph.graph import END, START

from ccp.agent import core
from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.core import GraphSpec, ModelDeps, NodePolicies, NodeSpec
from ccp.agent.state import AgentState
from ccp.agent.tools import BoundTool
from ccp.evaluators import citations, confidence
from ccp.evaluators.extraction import SchemaMeta
from ccp.evaluators.extraction.investigation import (
    GATHER_PROMPT_VERSION,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    InvestigationDraft,
)
from ccp.knowledge import graph as knowledge
from ccp.knowledge.actions import CATALOGUE
from ccp.tenancy import TenantContext

GRAPH_NAME = "investigator"
TRAJECTORY = (
    "load_context",
    "gather",
    "diagnose",
    "verify_citations",
    "bound_actions",
    "finalize",
)
DRAFT_META = SchemaMeta(
    name="InvestigationDraft",
    model=InvestigationDraft,
    version=SCHEMA_VERSION,
    required=("root_cause",),
    cited=(),
    value_in_quote=(),
)


def _on_tool_result(state: AgentState, tool: str, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("is_error"):
        return {}
    chunks: dict[str, dict[str, Any]] = {}
    coverage: dict[str, bool] = {}
    payload = result.get("result", {})
    for c in payload.get("chunks", []) if isinstance(payload, dict) else []:
        chunks[str(c["chunk_id"])] = dict(c)
        coverage[str(c.get("evidence_type"))] = True
    return {"retrieved_chunks": chunks, "evidence_coverage": coverage}


def build_spec(
    tenant: TenantContext,
    tools: dict[str, BoundTool],
    deps: ModelDeps,
    *,
    now: datetime,
) -> GraphSpec:
    allowed = ALLOWLISTS[GRAPH_NAME]
    policies = NodePolicies.default()
    graph_ref = knowledge.load()

    def load_context(state: AgentState) -> dict[str, Any]:
        verdict_id = str(state.get("verdict_id"))
        verdict = tools["get_verdict"].call({"verdict_id": verdict_id})
        context: dict[str, Any] = {
            "verdict": verdict.get("result") or {"error": verdict.get("error")}
        }
        log = [
            {
                "node": "load_context",
                "tool": "get_verdict",
                "arguments": {"verdict_id": verdict_id},
                "is_error": bool(verdict.get("is_error")),
            }
        ]
        v = context["verdict"] if not verdict.get("is_error") else {}
        test_id = str(v.get("test_id") or state.get("test_id") or "")
        subject_key = str(v.get("subject_key") or state.get("subject_key") or "")
        if test_id and subject_key:
            current = tools["get_current_state"].call(
                {"test_id": test_id, "subject_key": subject_key}
            )
            context["current_state"] = current.get("result")
            log.append(
                {
                    "node": "load_context",
                    "tool": "get_current_state",
                    "arguments": {"test_id": test_id, "subject_key": subject_key},
                    "is_error": bool(current.get("is_error")),
                }
            )
        spec = graph_ref.tests.get(test_id)
        if spec is not None:
            context["test"] = {
                "test_id": spec.test_id,
                "title": spec.title,
                "evidence_types": [e.value for e in spec.evidence_types],
            }
            controls = []
            for control_id in spec.controls:
                control = tools["get_control"].call({"control_id": control_id})
                log.append(
                    {
                        "node": "load_context",
                        "tool": "get_control",
                        "arguments": {"control_id": control_id},
                        "is_error": bool(control.get("is_error")),
                    }
                )
                if not control.get("is_error"):
                    controls.append(control.get("result"))
            context["controls"] = controls
        subject = subject_key.split("|", 1)[1] if "|" in subject_key else subject_key
        if subject.startswith("person:"):
            canonical = subject.split(":", 1)[1]
            grants = tools["get_access_grants"].call({"canonical_person_id": canonical})
            context["grants"] = (grants.get("result") or {}).get("grants", [])
            log.append(
                {
                    "node": "load_context",
                    "tool": "get_access_grants",
                    "arguments": {"canonical_person_id": canonical},
                    "is_error": bool(grants.get("is_error")),
                }
            )
        return {"context": context, "tool_log": log}

    def goal(state: AgentState) -> str:
        context = state.get("context") or {}
        verdict = context.get("verdict") or {}
        return (
            f"Investigate verdict {state.get('verdict_id')}: test {verdict.get('test_id')} is"
            f" {verdict.get('status')} for {verdict.get('subject_key')}.\n"
            f"Reasoning: {verdict.get('reasoning')}\n"
            f"Evidence refs: {verdict.get('evidence_refs')}\n"
            "Known context: "
            + json.dumps({k: v for k, v in context.items() if k != "verdict"}, default=str)[:4000]
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
        context = state.get("context") or {}
        return (
            "Verdict under investigation (system data, not evidence):\n"
            + json.dumps(context, default=str)[:12000]
        )

    diagnose = core.make_structured_node(
        "diagnose",
        deps=deps,
        prompt_version=PROMPT_VERSION,
        meta=DRAFT_META,
        select_chunks=lambda s: list(s.get("retrieved_chunks", {}).values()),
        describe=describe,
        require_chunks=False,
    )

    def verify(state: AgentState) -> dict[str, Any]:
        raw = (state.get("extracted_raw") or {}).get("diagnose")
        chunks = state.get("retrieved_chunks", {})
        records: list[dict[str, Any]] = []
        events: list[str] = []
        fields = (raw or {}).get("fields") or {}
        given = 0
        verified = 0
        for cited in fields.get("citations") or []:
            given += 1
            chunk_id = str(cited.get("chunk_id", ""))
            quote = str(cited.get("quote", ""))[: citations.MAX_QUOTE_CHARS]
            chunk = chunks.get(chunk_id)
            ok = chunk is not None and citations.quote_supported(quote, str(chunk.get("text", "")))
            if not ok:
                kind = "unknown_chunk" if chunk is None else "quote_not_found"
                events.append(f"citation_{kind}:root_cause:{chunk_id}")
            else:
                verified += 1
            records.append(
                {"chunk_id": chunk_id, "quote": quote, "supports": "root_cause", "verified": ok}
            )
        stats = {
            "coverage": 1.0 if fields.get("root_cause") else 0.0,
            "citation_rate": verified / given if given else 1.0,
        }
        return {"verified_citations": records, "citation_stats": stats, "guardrail_events": events}

    def bound_actions(state: AgentState) -> dict[str, Any]:
        raw = (state.get("extracted_raw") or {}).get("diagnose")
        fields = (raw or {}).get("fields") or {}
        events: list[str] = []
        bounded: list[dict[str, Any]] = []
        for item in fields.get("proposed_actions") or []:
            action = str(item.get("action", ""))
            spec = CATALOGUE.get(action)
            if spec is None:
                events.append(f"action_rejected:unknown_action:{action}")
                continue
            target = {k: v for k, v in (item.get("target") or {}).items() if v}
            missing = [k for k in spec.required_target_keys if not target.get(k)]
            if missing:
                events.append(f"action_rejected:missing_target:{action}:{','.join(missing)}")
                continue
            bounded.append(
                {
                    "action": action,
                    "target": target,
                    "rationale": str(item.get("rationale", "")),
                    "reversible": spec.reversible,
                    "live_capable": spec.live_capable,
                }
            )
        draft = {
            "root_cause": str(
                fields.get("root_cause") or "the investigator could not determine a root cause"
            ),
            "root_cause_category": str(fields.get("root_cause_category") or "unknown"),
            "missing_evidence": list(fields.get("missing_evidence") or []),
            "proposed_actions": bounded,
        }
        if raw is None:
            events.append("diagnosis_missing")
        return {"draft_verdict": draft, "guardrail_events": events}

    def finalize(state: AgentState) -> dict[str, Any]:
        draft = dict(state.get("draft_verdict") or {})
        stats = state.get("citation_stats") or {"coverage": 0.0, "citation_rate": 1.0}
        injection = any(
            e.startswith("injection_marker_detected:") for e in state.get("guardrail_events", [])
        )
        conf = confidence.compute(
            coverage=float(stats["coverage"]),
            citation_rate=float(stats["citation_rate"]),
            retrieval_quality=1.0 if state.get("retrieved_chunks") else None,
            injection=injection,
            # a gather that stopped early (A-100) leaves the diagnosis unresolved
            unresolved_evidence_type=any(
                e.startswith("tool_loop_stopped:") for e in state.get("guardrail_events", [])
            ),
        )
        requests = state.get("requests", {})
        diagnose_call = requests.get("diagnose", {})
        final = {
            **draft,
            "citations": state.get("verified_citations") or [],
            "confidence": conf.value,
            "confidence_cap": conf.cap,
            "model": diagnose_call.get("model"),
            "prompt_version": PROMPT_VERSION,
            "guardrail_events": list(state.get("guardrail_events", [])) + list(conf.events),
            "thread_id": state.get("thread_id"),
            "request_json": {
                "thread_id": state.get("thread_id"),
                "calls": requests,
                "tool_log": state.get("tool_log", []),
            },
            "response_json": {"diagnose": diagnose_call.get("response")},
        }
        return {"final": final}

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
            node("load_context", load_context),
            node("gather", gather),
            node("diagnose", diagnose),
            node("verify_citations", verify),
            node("bound_actions", bound_actions),
            node("finalize", finalize),
        ),
        edges=(
            (START, "load_context"),
            ("load_context", "gather"),
            ("gather", "diagnose"),
            ("diagnose", "verify_citations"),
            ("verify_citations", "bound_actions"),
            ("bound_actions", "finalize"),
            ("finalize", END),
        ),
        output_schema=InvestigationDraft,
    )
