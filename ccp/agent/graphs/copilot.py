"""The Compliance Copilot graph (spec.md Layer 17), a declaration over the shared core.

    START -> classify          structured: intent in {explain, status, missing, act, other}
          -> gather            tool loop on the copilot allowlist (every read tool), <= 10 calls
          -> answer            structured -> CopilotAnswerDraft {text, citations, requested_actions}
          -> verify_citations  pure: citations checked against the chunks shown this turn; the
                               status contract enforced; requested actions bounded and capped
          -> END

One REPL session is one checkpointed thread; every turn runs the whole graph again on that
thread, so earlier turns' chunks and tool results stay in the checkpoint but only this turn's
are shown to the model. The graph has no write path: requested actions are data the runner
turns into approval items after an explicit confirmation.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from langgraph.graph import END, START
from pydantic import BaseModel, ConfigDict, Field

from ccp.agent import core
from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.core import GraphSpec, ModelDeps, NodePolicies, NodeSpec
from ccp.agent.state import AgentState
from ccp.agent.tools import BoundTool
from ccp.config import REPO_ROOT
from ccp.evaluators import citations
from ccp.evaluators.extraction import SchemaMeta
from ccp.evaluators.extraction.copilot import (
    ANSWER_PROMPT_VERSION,
    CLASSIFY_PROMPT_VERSION,
    GATHER_PROMPT_VERSION,
    INTENTS,
    SCHEMA_VERSION,
    CopilotAnswerDraft,
    IntentDraft,
)
from ccp.knowledge.actions import CATALOGUE
from ccp.security import prompt_guard
from ccp.tenancy import TenantContext

GRAPH_NAME = "copilot"
TRAJECTORY = ("classify", "gather", "answer", "verify_citations")
CONFIG_PATH = REPO_ROOT / "config" / "copilot.yaml"
CHUNK_TOOLS = frozenset({"search_evidence", "get_chunk", "get_evidence_chunks"})

CONTRACTS: dict[str, str] = {
    "explain": (
        "Call list_findings with the test id to find the subject's row, then get_verdict with"
        " its verdict_id. Answer by quoting the verdict's verified citations and name the tier"
        " and the model that produced it. Never restate the reasoning without citations."
    ),
    "status": (
        "Call get_framework_rollup for the framework asked about (soc2 or iso27001; both when"
        " unspecified), then list_findings with status=fail. Report the counts by status per"
        " requirement and the failing subjects. Never say 'ready' or 'compliant': the answer"
        " is a state of facts."
    ),
    "missing": (
        "Find the verdict (list_findings, then get_verdict), then get_investigation for it."
        " If an investigation exists, list its missing evidence; if none exists, say so and"
        " offer to open one. Never invent an evidence type."
    ),
    "act": (
        "Find the verdict (list_findings, get_verdict), read get_investigation and"
        " get_access_grants where relevant, and return requested_actions from the catalogue"
        " with concrete targets and the verdict_id. You cannot change anything: say that an"
        " approval will be opened for a reviewer. Never claim the fix is done."
    ),
    "other": (
        "Answer from the tools when the data supports it; otherwise say the question is"
        " outside the data available to you. Never speculate."
    ),
}

INTENT_META = SchemaMeta(
    name="IntentDraft",
    model=IntentDraft,
    version="copilot_intent_v1",
    required=("intent",),
    cited=(),
    value_in_quote=(),
)
ANSWER_META = SchemaMeta(
    name="CopilotAnswerDraft",
    model=CopilotAnswerDraft,
    version=SCHEMA_VERSION,
    required=("text",),
    cited=(),
    value_in_quote=(),
)


class CopilotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_actions_per_turn: int = Field(default=2, ge=0)
    max_tool_calls: int = Field(default=10, ge=1)
    forbidden_summary_words: tuple[str, ...] = ("ready", "compliant")


def load_config(path: Path = CONFIG_PATH) -> CopilotConfig:
    if not path.exists():
        return CopilotConfig()
    return CopilotConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def says_forbidden(text: str, forbidden: tuple[str, ...]) -> list[str]:
    """The forbidden summary words that appear (word-bounded, any case) in the text."""
    return [w for w in forbidden if re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE)]


def enforce_status_contract(text: str, forbidden: tuple[str, ...]) -> tuple[str, list[str]]:
    """Drop every sentence that uses a forbidden word; return the rest and the words found."""
    found: list[str] = []
    kept: list[str] = []
    for sentence in _SENTENCE.split(text.strip()):
        hits = says_forbidden(sentence, forbidden)
        if hits:
            found.extend(h for h in hits if h not in found)
            continue
        if sentence.strip():
            kept.append(sentence.strip())
    return " ".join(kept), found


def render_rollups(context: dict[str, Any]) -> str:
    """The deterministic status answer: the computed rollup with its blocking test (A-36)."""
    lines = ["Computed state (facts, not a judgement):"]
    rollups = [
        v
        for k, v in sorted(context.items())
        if k.startswith("get_framework_rollup:") and isinstance(v, dict)
    ]
    if not rollups:
        lines.append("- no framework rollup was retrieved this turn")
    for rollup in rollups:
        framework = rollup.get("framework_id")
        if rollup.get("no_evaluations_yet"):
            lines.append(f"- {framework}: no evaluations yet")
            continue
        for req in rollup.get("requirements") or []:
            counts = ", ".join(
                f"{status} {n}" for status, n in sorted((req.get("counts") or {}).items()) if n
            )
            blocking = ""
            if req.get("blocking_test_id"):
                blocking = (
                    f"; blocking test {req['blocking_test_id']}"
                    f" for {req.get('blocking_subject_key')}"
                )
            lines.append(
                f"- {framework} {req.get('requirement_id')}: {req.get('state')}"
                f" ({counts or 'no counts'}){blocking}"
            )
    failing = sorted(
        {
            f"{f.get('test_id')} for {f.get('subject_key')}"
            for k, v in context.items()
            if k.startswith("list_findings:") and isinstance(v, dict)
            for f in v.get("findings") or []
            if f.get("status") == "fail"
        }
    )
    if failing:
        lines.append("Failing subjects: " + "; ".join(failing))
    return "\n".join(lines)


def _result_key(tool: str, payload: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:10]
    return f"{tool}:{digest}"


def _on_tool_result(state: AgentState, tool: str, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("is_error"):
        return {}
    payload = result.get("result")
    turn = int(state.get("turn") or 0)
    if tool in CHUNK_TOOLS:
        chunks: dict[str, dict[str, Any]] = {}
        coverage: dict[str, bool] = {}
        if isinstance(payload, dict):
            items = [payload] if tool == "get_chunk" else list(payload.get("chunks") or [])
            for c in items:
                if not isinstance(c, dict) or "chunk_id" not in c:
                    continue
                chunks[str(c["chunk_id"])] = {**c, "turn": turn}
                coverage[str(c.get("evidence_type"))] = True
        return {"retrieved_chunks": chunks, "evidence_coverage": coverage}
    return {"context": {_result_key(tool, payload): payload}}


def build_spec(
    tenant: TenantContext,
    tools: dict[str, BoundTool],
    deps: ModelDeps,
    *,
    now: datetime,
    config: CopilotConfig,
    known_tests: tuple[str, ...],
) -> GraphSpec:
    allowed = ALLOWLISTS[GRAPH_NAME]
    policies = NodePolicies.default()

    def turn_of(state: AgentState) -> int:
        return int(state.get("turn") or 0)

    def turn_chunks(state: AgentState) -> list[dict[str, Any]]:
        turn = turn_of(state)
        return [c for c in state.get("retrieved_chunks", {}).values() if c.get("turn") == turn]

    def tool_results(state: AgentState) -> dict[str, Any]:
        return {k: v for k, v in (state.get("context") or {}).items() if ":" in k}

    def describe_question(state: AgentState) -> str:
        return (
            f"Question: {state.get('question')}\n"
            f"Known test ids: {', '.join(known_tests)}\n"
            "Frameworks: soc2, iso27001\n"
            "Classify the intent and pull out any test id, subject, or framework the question"
            " names."
        )

    inner_classify = core.make_structured_node(
        "classify",
        deps=deps,
        prompt_version=CLASSIFY_PROMPT_VERSION,
        meta=INTENT_META,
        select_chunks=lambda s: [],
        describe=describe_question,
        require_chunks=False,
    )

    def classify(state: AgentState) -> dict[str, Any]:
        out = inner_classify(state)
        fields = ((out.get("extracted_raw") or {}).get("classify") or {}).get("fields") or {}
        intent = str(fields.get("intent") or "other")
        if intent not in INTENTS:
            out.setdefault("guardrail_events", []).append(f"intent_unknown:{intent}")
            intent = "other"
        out["intent"] = intent
        out["context"] = {  # a fresh context per turn: earlier turns' results are not shown
            "question": state.get("question"),
            "intent": intent,
            "test_id": fields.get("test_id"),
            "subject_hint": fields.get("subject_hint"),
            "framework_id": fields.get("framework_id"),
        }
        return out

    def goal(state: AgentState) -> str:
        intent = state.get("intent") or "other"
        ctx = state.get("context") or {}
        return (
            f"Question from the tenant's operator: {state.get('question')}\n"
            f"Intent: {intent}\n"
            f"Hints: test_id={ctx.get('test_id')}, subject={ctx.get('subject_hint')},"
            f" framework={ctx.get('framework_id')}\n"
            f"Contract: {CONTRACTS.get(intent, CONTRACTS['other'])}\n"
            f"Known test ids: {', '.join(known_tests)}. Frameworks: soc2, iso27001.\n"
            "Use the tools to gather what the answer needs, then stop."
        )

    gather = core.make_tool_loop(
        "gather",
        deps=deps,
        tools=tools,
        allowed=allowed,
        prompt_version=GATHER_PROMPT_VERSION,
        goal=goal,
        on_result=_on_tool_result,
        max_calls=config.max_tool_calls,
    )

    def describe_answer(state: AgentState) -> str:
        intent = state.get("intent") or "other"
        results = json.dumps(tool_results(state), default=str, sort_keys=True)[:30000]
        return (
            f"Question: {state.get('question')}\n"
            f"Intent: {intent}\n"
            f"Contract: {CONTRACTS.get(intent, CONTRACTS['other'])}\n"
            f"TOOL_RESULTS_JSON: {results}\n"
            "Answer the operator's question. Cite only quotes that appear verbatim in the fenced"
            " blocks, with their chunk ids. Requested actions must use the catalogue names and"
            " concrete targets, and carry the verdict_id they fix."
        )

    def _answer_node(stream: bool) -> core.NodeFn:
        return core.make_structured_node(
            "answer",
            deps=deps,
            prompt_version=ANSWER_PROMPT_VERSION,
            meta=ANSWER_META,
            select_chunks=turn_chunks,
            describe=describe_answer,
            require_chunks=False,
            stream=stream,
        )

    # The status contract may drop sentences after generation, so a status answer is never
    # streamed to the terminal; every other intent streams its prose as it is generated.
    inner_answer_streamed = _answer_node(True)
    inner_answer_plain = _answer_node(False)

    def answer(state: AgentState) -> dict[str, Any]:
        # The explain contract quotes a verdict's verified citations. Their chunks are fetched
        # here (read-only, this tenant) so the quotes are verified against text the model saw.
        turn = turn_of(state)
        known = dict(state.get("retrieved_chunks", {}))
        fetched: dict[str, dict[str, Any]] = {}
        log: list[dict[str, Any]] = []
        for key, payload in (state.get("context") or {}).items():
            if not key.startswith("get_verdict:") or not isinstance(payload, dict):
                continue
            for cited in payload.get("citations") or []:
                chunk_id = str(cited.get("chunk_id", ""))
                if not chunk_id or chunk_id in fetched or "get_chunk" not in tools:
                    continue
                if chunk_id in known:
                    if known[chunk_id].get("turn") != turn:
                        fetched[chunk_id] = {**known[chunk_id], "turn": turn}
                    continue
                res = tools["get_chunk"].call({"chunk_id": chunk_id})
                log.append(
                    {
                        "node": "answer",
                        "tool": "get_chunk",
                        "arguments": {"chunk_id": chunk_id},
                        "is_error": bool(res.get("is_error")),
                    }
                )
                got = res.get("result")
                if not res.get("is_error") and isinstance(got, dict) and "chunk_id" in got:
                    fetched[chunk_id] = {**got, "turn": turn}
        view: Any = {**state, "retrieved_chunks": {**known, **fetched}} if fetched else state
        # tool results enter the prompt as system data outside the fence: scan them too
        flagged = prompt_guard.scan(json.dumps(tool_results(state), default=str))
        inner = inner_answer_plain if state.get("intent") == "status" else inner_answer_streamed
        out = inner(view)
        if flagged:
            out.setdefault("guardrail_events", []).extend(
                f"injection_marker_detected:tool_results:{phrase}" for phrase in flagged
            )
        if fetched:
            out["retrieved_chunks"] = fetched
        if log:
            out["tool_log"] = log
        return out

    def verify(state: AgentState) -> dict[str, Any]:
        raw = ((state.get("extracted_raw") or {}).get("answer") or {}).get("fields") or {}
        chunks = {str(c["chunk_id"]): c for c in turn_chunks(state)}
        intent = state.get("intent") or "other"
        ctx = state.get("context") or {}
        events: list[str] = []
        records: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        for cited in raw.get("citations") or []:
            chunk_id = str(cited.get("chunk_id", ""))
            quote = str(cited.get("quote", ""))[: citations.MAX_QUOTE_CHARS]
            chunk = chunks.get(chunk_id)
            ok = chunk is not None and citations.quote_supported(quote, str(chunk.get("text", "")))
            record = {"chunk_id": chunk_id, "quote": quote, "supports": "answer", "verified": ok}
            records.append(record)
            if ok:
                kept.append(record)
            else:
                kind = "unknown_chunk" if chunk is None else "quote_not_found"
                events.append(f"citation_{kind}:{chunk_id}")
        text = str(raw.get("text") or "").strip()
        if not raw:
            events.append("answer_missing")
            text = text or "I could not produce an answer for that question."
        if intent == "status":
            text, dropped = enforce_status_contract(text, config.forbidden_summary_words)
            events.extend(f"forbidden_summary_word:{w}" for w in dropped)
            text = (text + "\n\n" if text else "") + render_rollups(ctx)
        bounded: list[dict[str, Any]] = []
        for item in raw.get("requested_actions") or []:
            action = str(item.get("action", ""))
            spec = CATALOGUE.get(action)
            if spec is None:
                events.append(f"action_rejected:unknown_action:{action}")
                continue
            target = {str(k): str(v) for k, v in (item.get("target") or {}).items() if v}
            missing = [k for k in spec.required_target_keys if not target.get(k)]
            if missing:
                events.append(f"action_rejected:missing_target:{action}:{','.join(missing)}")
                continue
            bounded.append(
                {
                    "action": action,
                    "target": target,
                    "rationale": str(item.get("rationale", "")),
                    "verdict_id": item.get("verdict_id"),
                    "reversible": spec.reversible,
                    "live_capable": spec.live_capable,
                }
            )
        cap = config.max_actions_per_turn
        if len(bounded) > cap:
            extra = len(bounded) - cap
            bounded = bounded[:cap]
            events.append(f"requested_actions_capped:{extra}")
            text += (
                f"\n\n{extra} further requested action(s) were not included: at most {cap}"
                " per turn. Ask again for the rest."
            )
        if bounded:
            text += (
                "\n\nNothing has been changed. Each requested action becomes an approval item"
                " for a reviewer once you confirm."
            )
        requests = state.get("requests", {})
        final = {
            "turn": turn_of(state),
            "question": state.get("question"),
            "intent": intent,
            "test_id": ctx.get("test_id"),
            "text": text,
            "citations": kept,
            "citation_records": records,
            "requested_actions": bounded,
            "guardrail_events": events,
            "model": (requests.get("answer") or {}).get("model"),
            "prompt_version": ANSWER_PROMPT_VERSION,
            "fence_nonce": state.get("fence_nonce"),
            "request_json": {"calls": requests, "retrieved_chunk_ids": sorted(chunks)},
            "response_json": {
                "classify": (requests.get("classify") or {}).get("response"),
                "answer": (requests.get("answer") or {}).get("response"),
            },
        }
        return {"final": final, "verified_citations": records, "guardrail_events": events}

    def node(name: str, fn: core.NodeFn) -> NodeSpec:
        wrapped = core.with_policies(fn, name, tenant, deps.tracker, policies.timeout_seconds)
        return NodeSpec(name, wrapped, policies)

    return GraphSpec(
        name=GRAPH_NAME,
        tools=tuple(sorted(tools)),
        nodes=(
            node("classify", classify),
            node("gather", gather),
            node("answer", answer),
            node("verify_citations", verify),
        ),
        edges=(
            (START, "classify"),
            ("classify", "gather"),
            ("gather", "answer"),
            ("answer", "verify_citations"),
            ("verify_citations", END),
        ),
        conditional_edges=(),
        output_schema=CopilotAnswerDraft,
    )
