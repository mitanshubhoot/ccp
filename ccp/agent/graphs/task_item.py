"""The task item graph (spec.md A-151, report 6.8 "The fan-out contract"): one fresh run over
one item (fanned out) or a few (a single-run baseline), never the parent task's history.

    START -> load_context   code: the items' typed facts, the verdicts and controls they name
          -> gather         the bounded tool loop on the task_item allowlist (read-only)
          -> answer         one structured call -> ItemReadings, one reading per item
          -> verify         code: each reading's item id is one of this run's, once; every
                            claim's quote is in its chunk, or the claim is dropped; a verified
                            claim becomes a pointer (evidence, chunk, offsets); a reading over
                            RESULT_TOKEN_CAP loses claims from the end until it fits
          -> correct        once, when a check failed: the answer again with the typed
                            failures, then verify again; an item that fails again is
                            held for review (A-152)
          -> finalize -> END

The run returns only the verified readings (ids, offsets, fields, a one-line reason); the
task runner merges them by item id. No tool here writes, and none starts another run.
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.graph import END, START

from ccp.agent import core
from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.core import ConditionalEdge, GraphSpec, ModelDeps, NodePolicies, NodeSpec
from ccp.agent.state import AgentState
from ccp.agent.tools import BoundTool
from ccp.evaluators import citations
from ccp.evaluators.extraction import SchemaMeta
from ccp.evaluators.extraction.task_item import (
    GATHER_PROMPT_VERSION,
    PROMPT_VERSION,
    RESULT_TOKEN_CAP,
    SCHEMA_VERSION,
    ItemReadings,
)
from ccp.tenancy import TenantContext

GRAPH_NAME = "task_item"
TRAJECTORY = ("load_context", "gather", "answer", "verify", "finalize")
TRAJECTORY_CORRECTED = (
    "load_context",
    "gather",
    "answer",
    "verify",
    "correct",
    "verify",
    "finalize",
)
MAX_VERDICTS = 5  # verdict handles read up front, per item
READINGS_META = SchemaMeta(
    name="ItemReadings",
    model=ItemReadings,
    version=SCHEMA_VERSION,
    required=("readings",),
    cited=(),
    value_in_quote=(),
)


def tokens_of(value: Any) -> int:
    """A rough count (four characters a token): enough to hold a result to its ceiling."""
    return -(-len(json.dumps(value, default=str, ensure_ascii=False)) // 4)


def _on_tool_result(state: AgentState, tool: str, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("is_error"):
        return {}
    payload = result.get("result", {})
    if not isinstance(payload, dict):
        return {}
    chunks: dict[str, dict[str, Any]] = {}
    found = list(payload.get("chunks") or []) if tool != "read_span" else []
    if tool == "get_chunk" and payload.get("chunk_id"):
        found.append(payload)
    for c in found:
        if isinstance(c, dict) and c.get("chunk_id") and "text" in c:
            chunks[str(c["chunk_id"])] = dict(c)
    if tool == "read_span" and payload.get("text"):  # an exact passage is citable as read
        span_id = (
            f"span:{payload['evidence_entity_id']}:{payload['start_offset']}-"
            f"{payload['end_offset']}"
        )
        chunks[span_id] = {
            "chunk_id": span_id,
            "evidence_entity_id": payload["evidence_entity_id"],
            "evidence_type": payload.get("evidence_type"),
            "subject_entity_id": None,
            "page": payload.get("page"),
            "source_ref": f"{payload['evidence_entity_id']}@{payload.get('version')}",
            "text": payload["text"],
            "start_offset": payload["start_offset"],
            "end_offset": payload["end_offset"],
        }
    coverage = {str(c.get("evidence_type")): True for c in chunks.values()}
    return {"retrieved_chunks": chunks, "evidence_coverage": coverage}


def pointer(chunk: dict[str, Any], quote: str) -> dict[str, Any]:
    """Where a verified quote sits: its evidence, chunk and document offsets when known."""
    text = str(chunk.get("text", ""))
    at = text.find(quote)
    start = chunk.get("start_offset")
    begin = start + at if at >= 0 and isinstance(start, int) else None
    return {
        "evidence_entity_id": chunk.get("evidence_entity_id"),
        "chunk_id": str(chunk.get("chunk_id")),
        "start_offset": begin,
        "end_offset": begin + len(quote) if begin is not None else None,
    }


def check_readings(
    fields: dict[str, Any] | None,
    item_ids: list[str],
    chunks: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """The code checks between a run and a merge (5.4): (readings by item, rejections,
    events). A reading for an item not in the run, or a second one, is a merge rejection."""
    readings: dict[str, dict[str, Any]] = {}
    rejections: list[str] = []
    events: list[str] = []
    for raw in (fields or {}).get("readings") or []:
        item_id = str(raw.get("item_id", ""))
        if item_id not in item_ids:
            rejections.append(f"unknown_item:{item_id}")
            continue
        if item_id in readings:
            rejections.append(f"duplicate_reading:{item_id}")
            continue
        verified: list[dict[str, Any]] = []
        for finding in raw.get("findings") or []:
            cited = finding.get("citation") or {}
            chunk_id = str(cited.get("chunk_id", ""))
            quote = str(cited.get("quote", ""))[: citations.MAX_QUOTE_CHARS]
            chunk = chunks.get(chunk_id)
            if chunk is None:
                events.append(f"citation_unknown_chunk:{item_id}:{chunk_id}")
                continue
            if not citations.quote_supported(quote, str(chunk.get("text", ""))):
                events.append(f"citation_quote_not_found:{item_id}:{chunk_id}")
                continue
            verified.append(
                {"statement": str(finding.get("statement", "")), "quote": quote}
                | pointer(chunk, quote)
            )
        reading = {
            "item_id": item_id,
            "answer": str(raw.get("answer")),
            "reason": str(raw.get("reason", ""))[:300],
            "findings": verified,
            "missing": [str(m)[:200] for m in raw.get("missing") or []][:5],
            "dropped_claims": len(raw.get("findings") or []) - len(verified),
        }
        while verified and tokens_of(reading) > RESULT_TOKEN_CAP:
            verified.pop()
            reading["findings"] = verified
            events.append(f"result_trimmed:{item_id}")
        readings[item_id] = reading
    for item_id in item_ids:
        if item_id not in readings:
            events.append(f"no_reading:{item_id}")
    return readings, rejections, events


def build_spec(tenant: TenantContext, tools: dict[str, BoundTool], deps: ModelDeps) -> GraphSpec:
    allowed = ALLOWLISTS[GRAPH_NAME]
    policies = NodePolicies.default()

    def items_of(state: AgentState) -> list[dict[str, Any]]:
        return list(state.get("task_items") or [])

    def load_context(state: AgentState) -> dict[str, Any]:
        context: dict[str, Any] = {}
        log: list[dict[str, Any]] = []
        for item in items_of(state):
            known: dict[str, Any] = {}
            inputs = dict(item.get("inputs") or {})
            for verdict_id in list(inputs.get("verdict_ids") or [])[:MAX_VERDICTS]:
                found = tools["get_verdict"].call({"verdict_id": str(verdict_id)})
                log.append(
                    {
                        "node": "load_context",
                        "tool": "get_verdict",
                        "arguments": {"verdict_id": str(verdict_id)},
                        "is_error": bool(found.get("is_error")),
                    }
                )
                if not found.get("is_error"):
                    v = found.get("result") or {}
                    known.setdefault("verdicts", []).append(
                        {k: v.get(k) for k in ("verdict_id", "test_id", "subject_key", "status")}
                        | {"reasoning": str(v.get("reasoning") or "")[:400]}
                    )
            control_id = inputs.get("control_id")
            if control_id:
                control = tools["get_control"].call({"control_id": str(control_id)})
                log.append(
                    {
                        "node": "load_context",
                        "tool": "get_control",
                        "arguments": {"control_id": str(control_id)},
                        "is_error": bool(control.get("is_error")),
                    }
                )
                if not control.get("is_error"):
                    known["control"] = control.get("result")
            context[str(item["item_id"])] = known
        return {"context": context, "tool_log": log}

    def brief(state: AgentState) -> str:
        """The items as typed facts: question, inputs, what code already resolved."""
        context = state.get("context") or {}
        lines = [f"Task {(state.get('task') or {}).get('task_id')}: answer each item."]
        for item in items_of(state):
            item_id = str(item["item_id"])
            facts = {k: v for k, v in dict(item.get("inputs") or {}).items() if k != "text"}
            lines.append(f"Item {item_id}: {item.get('question')}")
            lines.append("  Facts (system data): " + json.dumps(facts, default=str)[:2000])
            if context.get(item_id):
                lines.append(
                    "  Known (system data): " + json.dumps(context[item_id], default=str)[:2000]
                )
        return "\n".join(lines)

    gather = core.make_tool_loop(
        "gather",
        deps=deps,
        tools=tools,
        allowed=allowed,
        prompt_version=GATHER_PROMPT_VERSION,
        goal=brief,
        on_result=_on_tool_result,
    )

    def describe(state: AgentState) -> str:
        ids = [str(i["item_id"]) for i in items_of(state)]
        return brief(state) + "\n\nReturn one reading for each of these item ids: " + ", ".join(ids)

    answer = core.make_structured_node(
        "answer",
        deps=deps,
        prompt_version=PROMPT_VERSION,
        meta=READINGS_META,
        select_chunks=lambda s: list(s.get("retrieved_chunks", {}).values()),
        describe=describe,
        require_chunks=False,
    )

    def verify(state: AgentState) -> dict[str, Any]:
        raw = (state.get("extracted_raw") or {}).get("answer")
        ids = [str(i["item_id"]) for i in items_of(state)]
        readings, rejections, events = check_readings(
            (raw or {}).get("fields"), ids, state.get("retrieved_chunks", {})
        )
        schema = core.schema_failures(state.get("extracted_raw") or {}, None)
        failed = sorted(  # the items a failed check touches, by id, never parsed from text
            i
            for i in ids
            if schema
            or i not in readings
            or readings[i]["dropped_claims"] > 0
            or f"duplicate_reading:{i}" in rejections
        )
        failures = core.failures_in(events) + schema + [f"merge_rejected:{r}" for r in rejections]
        return {
            "extracted": {"readings": readings, "rejections": rejections, "failed_items": failed},
            "guardrail_events": events + core.after_correction(state, failures),
            "check_failures": failures,
        }

    correct = core.make_correction_node(
        "correct",
        deps=deps,
        targets={
            "answer": core.Correction(
                PROMPT_VERSION,
                READINGS_META,
                lambda s: list(s.get("retrieved_chunks", {}).values()),
                describe,
                lambda failure: True,
            )
        },
    )

    def finalize(state: AgentState) -> dict[str, Any]:
        extracted = state.get("extracted") or {}
        events = list(state.get("guardrail_events", []))
        stopped = next(
            (e.split(":")[-1] for e in events if e.startswith("tool_loop_stopped:")), None
        )
        call = (state.get("requests") or {}).get("answer", {})
        return {
            "final": {
                "readings": extracted.get("readings") or {},
                "rejections": extracted.get("rejections") or [],
                # after one correction round, an item a check still fails is held (A-152)
                "failed_items": (
                    extracted.get("failed_items") or [] if core.correction_failed(state) else []
                ),
                "corrections": int(state.get("corrections") or 0),
                "stopped": stopped,
                "guardrail_events": events,
                "model": call.get("model"),
                "prompt_version": PROMPT_VERSION,
                "bundle_sha256": call.get("bundle_sha256"),
                "thread_id": state.get("thread_id"),
            }
        }

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
            node("answer", answer),
            node("verify", verify),
            node("correct", correct),
            node("finalize", finalize),
        ),
        edges=(
            (START, "load_context"),
            ("load_context", "gather"),
            ("gather", "answer"),
            ("answer", "verify"),
            ("correct", "verify"),
            ("finalize", END),
        ),
        conditional_edges=(
            ConditionalEdge(
                "verify", core.correction_route, {"correct": "correct", "next": "finalize"}
            ),
        ),
        output_schema=ItemReadings,
    )
