"""Tier 3 as a fixed workflow with a bounded fallback (spec.md Layer 8 "Nodes and edges", R6).

    START -> fetch_critical_list -> extract_systems -> fetch_latest_review -> extract_scope
          -> verify_citations -> compare -> gate -> finalize -> END
    fetch_* -> gather_fallback (only when the direct fetch found nothing) -> extract_*
    gate    -> fetch_latest_review on a "regather" decision; interrupt() when the draft
               needs a human (status needs_review or confidence below the threshold); a
               missing or malformed decision holds the draft as needs_review (A-95)

Predictable steps get a workflow; the tool loop exists only as a fallback and its use is a
guardrail event. Nothing here persists anything: the wrapper in ccp/agent/tier3.py writes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from langgraph.graph import END, START
from langgraph.types import interrupt

from ccp.agent import core
from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.core import ConditionalEdge, GraphSpec, ModelDeps, NodePolicies, NodeSpec
from ccp.agent.state import AgentState
from ccp.agent.tools import BoundTool
from ccp.evaluators import confidence, rules
from ccp.evaluators.extraction import schema_for
from ccp.evaluators.thresholds import interrupt_threshold
from ccp.evaluators.verdict import Verdict, evaluation_key
from ccp.knowledge.graph import TestSpec
from ccp.retrieval.chunker import CHUNKER_VERSION
from ccp.tenancy import TenantContext
from ccp.types import VerdictStatus

GRAPH_NAME = "tier3"
CRITICAL = "critical_systems_list"
REVIEW = "access_review_report"
SYSTEMS_PROMPT = "hr4_systems_v1"
FALLBACK_PROMPT = "gather_fallback_v1"
TRAJECTORY_HAPPY = (
    "fetch_critical_list",
    "extract_systems",
    "fetch_latest_review",
    "extract_scope",
    "verify_citations",
    "compare",
    "gate",
    "finalize",
)


DECISION_ACTIONS = ("approve", "override", "regather")
NO_DECISION = "no valid reviewer decision; held for review"


def apply_decision(
    draft: dict[str, Any], decision: Any
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Apply a reviewer decision to the draft, failing closed: only a known action from a
    named reviewer changes anything, and anything else holds the draft as needs_review.
    Nothing defaults to approve. Returns (draft, human_decision, guardrail events)."""
    draft = dict(draft)
    received = dict(decision) if isinstance(decision, dict) else {}
    action = received.get("action")
    reviewer = str(received.get("reviewer") or "").strip()
    note = str(received.get("note") or "")
    status = received.get("status")
    new_status: str | None = None
    valid = action in DECISION_ACTIONS and bool(reviewer)
    if valid and action == "override":
        try:
            new_status = VerdictStatus(str(status)).value
        except ValueError:
            valid = False
    elif valid and status is not None:
        valid = False  # a status means an override; anything else is ambiguous
    if not valid:
        draft["status"] = VerdictStatus.needs_review.value
        draft["reasons"] = list(draft.get("reasons", [])) + [NO_DECISION]
        return draft, {"action": "none", "received": received}, ["no_decision"]
    if action == "override":
        draft["status"] = new_status
        draft["reasons"] = list(draft.get("reasons", [])) + [
            f"human override by {reviewer}: {note or 'no note'}"
        ]
        return draft, received, [f"human_override:{new_status}"]
    if action == "regather":
        return draft, received, ["human_regather"]
    draft["reasons"] = list(draft.get("reasons", [])) + [
        f"approved as {draft.get('status')} by {reviewer}"
    ]
    return draft, received, ["human_approved"]


def _chunks_of(state: AgentState, evidence_type: str) -> list[dict[str, Any]]:
    return [
        c
        for c in state.get("retrieved_chunks", {}).values()
        if c.get("evidence_type") == evidence_type
    ]


def _fetch_newest(tools: dict[str, BoundTool], evidence_type: str, node: str) -> core.NodeFn:
    """Pure tool calls: the newest evidence row of the type, then every chunk of it."""

    def node_fn(state: AgentState) -> dict[str, Any]:
        listing = tools["list_evidence"].call({"evidence_type": evidence_type})
        rows = listing.get("result", {}).get("evidence", []) if not listing.get("is_error") else []
        log = [
            {
                "node": node,
                "tool": "list_evidence",
                "arguments": {"evidence_type": evidence_type},
                "is_error": bool(listing.get("is_error")),
            }
        ]
        chunks: dict[str, dict[str, Any]] = {}
        if rows:
            newest = rows[0]
            fetched = tools["get_evidence_chunks"].call(
                {"evidence_entity_id": newest["evidence_entity_id"]}
            )
            log.append(
                {
                    "node": node,
                    "tool": "get_evidence_chunks",
                    "arguments": {"evidence_entity_id": newest["evidence_entity_id"]},
                    "is_error": bool(fetched.get("is_error")),
                }
            )
            for c in fetched.get("result", {}).get("chunks", []):
                chunks[str(c["chunk_id"])] = dict(c)
        return {
            "retrieved_chunks": chunks,
            "evidence_coverage": {evidence_type: bool(chunks)},
            "tool_log": log,
            "fallback_target": evidence_type
            if not chunks
            else "",  # "" clears; None would keep the old value
        }

    return node_fn


def _on_search_result(state: AgentState, tool: str, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("is_error"):
        return {}
    chunks = {}
    coverage: dict[str, bool] = {}
    for c in result.get("result", {}).get("chunks", []):
        chunks[str(c["chunk_id"])] = dict(c)
        coverage[str(c.get("evidence_type"))] = True
    return {"retrieved_chunks": chunks, "evidence_coverage": coverage}


def build_spec(
    tenant: TenantContext,
    spec: TestSpec,
    tools: dict[str, BoundTool],
    deps: ModelDeps,
    *,
    now: datetime,
) -> GraphSpec:
    assert spec.prompt_version and spec.extraction_schema
    systems_meta = schema_for("CriticalSystemsFields")
    scope_meta = schema_for(spec.extraction_schema)
    allowed = ALLOWLISTS[GRAPH_NAME]
    policies = NodePolicies.default()
    tracker = deps.tracker

    def describe(state: AgentState) -> str:
        return f"Scope: the whole tenant {tenant.settings.display_name}"

    extract_systems = core.make_structured_node(
        "extract_systems",
        deps=deps,
        prompt_version=SYSTEMS_PROMPT,
        meta=systems_meta,
        select_chunks=lambda s: _chunks_of(s, CRITICAL),
        describe=describe,
    )
    extract_scope = core.make_structured_node(
        "extract_scope",
        deps=deps,
        prompt_version=spec.prompt_version,
        meta=scope_meta,
        select_chunks=lambda s: _chunks_of(s, REVIEW),
        describe=describe,
    )
    gather_fallback_loop = core.make_tool_loop(
        "gather_fallback",
        deps=deps,
        tools=tools,
        allowed=allowed,
        prompt_version=FALLBACK_PROMPT,
        goal=lambda s: (
            f"The direct fetch found no evidence of type {s.get('fallback_target')} for tenant"
            f" {tenant.settings.display_name}. Search for it with the tools offered, then report."
        ),
        on_result=_on_search_result,
    )

    def gather_fallback(state: AgentState) -> dict[str, Any]:
        target = str(state.get("fallback_target"))
        out = gather_fallback_loop(state)
        out["guardrail_events"] = list(out.get("guardrail_events", [])) + [
            f"gather_fallback_used:{target}"
        ]
        out["fallback_used"] = [target]
        return out

    verify = core.make_verify_node(
        "verify_citations",
        {
            "extract_systems": ("systems", "critical_systems", systems_meta),
            "extract_scope": ("systems_in_scope", "systems_in_scope", scope_meta),
        },
    )

    def compare(state: AgentState) -> dict[str, Any]:
        extracted = state.get("extracted", {})
        decision = rules.decide(spec.rule, extracted, dict(spec.parameters), now)
        coverage = state.get("evidence_coverage", {})
        stats = state.get("citation_stats") or {"coverage": 0.0, "citation_rate": 1.0}
        unresolved = any(not coverage.get(t, False) for t in (CRITICAL, REVIEW))
        injection = any(
            e.startswith("injection_marker_detected:") for e in state.get("guardrail_events", [])
        )
        conf = confidence.compute(
            coverage=float(stats["coverage"]),
            citation_rate=float(stats["citation_rate"]),
            retrieval_quality=None if state.get("fallback_used") else 1.0,
            injection=injection,
            unresolved_evidence_type=unresolved,
        )
        status = conf.forced_status or decision.status
        return {
            "draft_verdict": {
                "status": status.value,
                "reasons": list(decision.reasons),
                "confidence": conf.value,
                "confidence_cap": conf.cap,
                "factors": {
                    "coverage": conf.coverage,
                    "citation_rate": conf.citation_rate,
                    "retrieval": conf.retrieval_quality,
                },
            },
            "confidence_cap": conf.cap,
            "guardrail_events": list(conf.events),
        }

    def gate(state: AgentState) -> dict[str, Any]:
        draft = dict(state.get("draft_verdict") or {})
        needs_human = (
            draft.get("status") == "needs_review"
            or float(draft.get("confidence", 0.0)) < interrupt_threshold()
        )
        if not needs_human:
            return {"human_decision": {"action": "auto"}}
        payload = {
            "thread_id": state.get("thread_id"),
            "test_id": state.get("test_id"),
            "subject_key": state.get("subject_key"),
            "draft": draft,
            "citations": state.get("verified_citations") or [],
            "evidence_coverage": state.get("evidence_coverage", {}),
            "guardrail_events": state.get("guardrail_events", []),
            "tool_log": state.get("tool_log", []),
            "threshold": interrupt_threshold(),
        }
        decision = interrupt(payload)  # no side effects above this line: the node re-runs on resume
        draft, applied, events = apply_decision(draft, decision)
        return {"human_decision": applied, "draft_verdict": draft, "guardrail_events": events}

    def finalize(state: AgentState) -> dict[str, Any]:
        draft = dict(state.get("draft_verdict") or {})
        subject_key = str(state.get("subject_key"))
        trigger = datetime.fromisoformat(str(state.get("trigger_observed_at")))
        reason = str(state.get("trigger_reason", "impact"))
        model = str(state.get("model"))
        shown = sorted(state.get("retrieved_chunks", {}))
        evidence_refs = sorted(
            {str(c.get("evidence_entity_id")) for c in state.get("retrieved_chunks", {}).values()}
        )
        verdict = Verdict(
            evaluation_key=evaluation_key(
                tenant.tenant_id, spec.test_id, subject_key, trigger, spec.prompt_version, model
            ),
            tenant_id=tenant.tenant_id,
            test_id=spec.test_id,
            subject_key=subject_key,
            status=VerdictStatus(str(draft["status"])),
            reasoning="; ".join(draft.get("reasons", []))
            + f". confidence {draft.get('confidence'):.2f} (cap {draft.get('confidence_cap'):.2f})",
            citations=tuple(
                {
                    "chunk_id": c["chunk_id"],
                    "quote": c["quote"],
                    "supports": c["supports"],
                    "verified": c["verified"],
                }
                for c in (state.get("verified_citations") or [])
            ),
            evidence_refs=tuple(evidence_refs),
            confidence=float(draft.get("confidence", 0.0)),
            confidence_cap=float(draft.get("confidence_cap", 1.0)),
            tier=3,
            model=model,
            prompt_version=spec.prompt_version,
            retrieved_chunk_ids=tuple(shown),
            guardrail_events=tuple(state.get("guardrail_events", [])),
            extracted_fields={
                **state.get("extracted", {}),
                "systems_prompt_version": SYSTEMS_PROMPT,
            },
            chunker_version=CHUNKER_VERSION,
            schema_version=scope_meta.version,
            fence_nonce=state.get("fence_nonce"),
            trigger_observed_at=trigger,
            trigger_reason=reason,
            valid_from=trigger,
            request_json={"thread_id": state.get("thread_id"), "calls": state.get("requests", {})},
            response_json={
                "human_decision": state.get("human_decision"),
                "tool_log": state.get("tool_log", []),
            },
            created_at=datetime.fromisoformat(str(state.get("now"))),
        )
        return {"final_verdict": verdict.model_dump(mode="json")}

    def after_fetch(state: AgentState) -> str:
        return "gather_fallback" if state.get("fallback_target") else "extract"

    def after_fallback(state: AgentState) -> str:
        return "extract_systems" if state.get("fallback_target") == CRITICAL else "extract_scope"

    def after_gate(state: AgentState) -> str:
        return (
            "regather"
            if (state.get("human_decision") or {}).get("action") == "regather"
            else "finalize"
        )

    def node(name: str, fn: core.NodeFn) -> NodeSpec:
        return NodeSpec(
            name,
            core.with_policies(fn, name, tenant, tracker, policies.timeout_seconds),
            policies,
        )

    return GraphSpec(
        name=GRAPH_NAME,
        tools=tuple(sorted(tools)),
        nodes=(
            node("fetch_critical_list", _fetch_newest(tools, CRITICAL, "fetch_critical_list")),
            node("gather_fallback", gather_fallback),
            node("extract_systems", extract_systems),
            node("fetch_latest_review", _fetch_newest(tools, REVIEW, "fetch_latest_review")),
            node("extract_scope", extract_scope),
            node("verify_citations", verify),
            node("compare", compare),
            node("gate", gate),
            node("finalize", finalize),
        ),
        edges=(
            (START, "fetch_critical_list"),
            ("extract_systems", "fetch_latest_review"),
            ("extract_scope", "verify_citations"),
            ("verify_citations", "compare"),
            ("compare", "gate"),
            ("finalize", END),
        ),
        conditional_edges=(
            ConditionalEdge(
                "fetch_critical_list",
                after_fetch,
                {"gather_fallback": "gather_fallback", "extract": "extract_systems"},
            ),
            ConditionalEdge(
                "fetch_latest_review",
                after_fetch,
                {"gather_fallback": "gather_fallback", "extract": "extract_scope"},
            ),
            ConditionalEdge(
                "gather_fallback",
                after_fallback,
                {"extract_systems": "extract_systems", "extract_scope": "extract_scope"},
            ),
            ConditionalEdge(
                "gate", after_gate, {"regather": "fetch_latest_review", "finalize": "finalize"}
            ),
        ),
        output_schema=Verdict,
    )
