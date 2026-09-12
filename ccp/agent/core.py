"""The shared agent core (spec.md Layer 8 "The shared agent core", R17): the one implementation
of what every graph needs. Graph modules are short declarations over these factories.

    GraphSpec ──► build_graph(spec, checkpointer)
      nodes: NodeSpec(name, fn, policies)      every node carries RetryPolicy + timeout, or the
                                               spec refuses to build
      make_structured_node(...)                fenced evidence -> schema -> raw fields + replay
      make_tool_loop(...)                      the bounded, allowlisted tool loop
      make_verify_node(...)                    citation verification over list-valued fields
      with_policies(fn, name, ...)             step log + budget clock around every node

    ┌──────────┐  retry (retryable errors only, max 3, backoff)   ┌──────────┐
    │  node fn │ ─────────────────────────────────────────────►  │ pipeline │
    └──────────┘  timeout per node; RunBudget over the whole run └──────────┘
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy
from pydantic import ValidationError

from ccp.agent.allowlists import check_allowlist
from ccp.agent.state import AgentState
from ccp.agent.tools import BoundTool
from ccp.evaluators import citations
from ccp.evaluators.extraction import SchemaMeta, schema_block
from ccp.ingest.consumers import RetryableError
from ccp.llm.budget import BudgetTracker
from ccp.llm.client import LLMClient, SystemBlock, tool_result_message
from ccp.llm.prompts import load_prompt
from ccp.observability import steps
from ccp.security import prompt_guard
from ccp.security.prompt_guard import FramedChunk
from ccp.tenancy import TenantContext

NodeFn = Callable[[AgentState], dict[str, Any]]
Router = Callable[[AgentState], str]


@dataclass(frozen=True)
class NodePolicies:
    retry: RetryPolicy
    timeout_seconds: float

    @classmethod
    def default(cls, timeout_seconds: float = 90.0) -> NodePolicies:
        return cls(
            retry=RetryPolicy(
                max_attempts=3,
                initial_interval=0.5,
                backoff_factor=2.0,
                jitter=True,
                retry_on=RetryableError,
            ),
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True)
class NodeSpec:
    name: str
    fn: NodeFn
    policies: NodePolicies | None = None


@dataclass(frozen=True)
class ConditionalEdge:
    source: str
    router: Router
    targets: dict[str, str]


@dataclass(frozen=True)
class GraphSpec:
    name: str  # a key of ALLOWLISTS
    tools: tuple[str, ...]
    nodes: tuple[NodeSpec, ...]
    edges: tuple[tuple[str, str], ...] = ()
    conditional_edges: tuple[ConditionalEdge, ...] = ()
    output_schema: type[Any] | None = None

    def validate(self) -> None:
        check_allowlist(self.name, self.tools)
        names = [n.name for n in self.nodes]
        if len(names) != len(set(names)):
            raise ValueError(f"graph {self.name!r}: duplicate node names")
        without = [n.name for n in self.nodes if n.policies is None]
        if without:
            raise ValueError(f"graph {self.name!r}: nodes without policies: {without}")
        known = set(names) | {START, END}
        for src, dst in self.edges:
            if src not in known or dst not in known:
                raise ValueError(
                    f"graph {self.name!r}: edge {src!r} -> {dst!r} names an unknown node"
                )
        for edge in self.conditional_edges:
            if edge.source not in known or any(t not in known for t in edge.targets.values()):
                raise ValueError(
                    f"graph {self.name!r}: conditional edge from {edge.source!r} is bad"
                )


def with_policies(
    fn: NodeFn,
    name: str,
    tenant: TenantContext,
    tracker: BudgetTracker,
    timeout_seconds: float | None = None,
) -> NodeFn:
    """Every node: budget clock first, then a step around the call, guardrail events logged,
    and a soft per-node time limit recorded as an event when exceeded."""

    def wrapped(state: AgentState) -> dict[str, Any]:
        tracker.check_clock()
        started = time.monotonic()
        with steps.step(
            f"agent.{name}",
            layer="agent",
            tenant=tenant,
            inputs={"thread_id": state.get("thread_id")},
        ) as s:
            out = fn(state)
            elapsed = time.monotonic() - started
            if timeout_seconds is not None and elapsed > timeout_seconds:
                out = {
                    **out,
                    "guardrail_events": list(out.get("guardrail_events", []))
                    + [f"node_timeout_exceeded:{name}:{elapsed:.1f}s"],
                }
            for event in out.get("guardrail_events", []):
                s.event(str(event))
            s.set_output({"updates": sorted(out), "elapsed_s": round(elapsed, 3)})
        return out

    wrapped.__name__ = f"node_{name}"
    return wrapped


def build_graph(spec: GraphSpec, checkpointer: Any) -> CompiledStateGraph[Any, Any, Any, Any]:
    spec.validate()
    graph: Any = StateGraph(AgentState)
    for node in spec.nodes:
        assert node.policies is not None
        # LangGraph can only cancel async nodes, so the per-node limit is enforced by
        # with_policies (soft: a guardrail event) and the run budget's wall clock (hard: the
        # next node boundary raises BudgetExceeded).
        graph.add_node(node.name, node.fn, retry_policy=node.policies.retry)
    for src, dst in spec.edges:
        graph.add_edge(src, dst)
    for edge in spec.conditional_edges:
        graph.add_conditional_edges(edge.source, edge.router, edge.targets)
    compiled: CompiledStateGraph[Any, Any, Any, Any] = graph.compile(checkpointer=checkpointer)
    return compiled


# -- factories ------------------------------------------------------------------------------


@dataclass
class ModelDeps:
    llm: LLMClient | None
    model: str
    tracker: BudgetTracker
    reasoning: bool = False
    stream: Callable[[str], None] | None = None  # token sink for nodes built with stream=True

    def require(self) -> LLMClient:
        if self.llm is None:
            raise RuntimeError("no model client configured for this node")
        return self.llm


def frame_state_chunks(
    chunks: list[dict[str, Any]], nonce: str
) -> tuple[prompt_guard.Framed, list[str]]:
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
    return framed, framed.shown_chunk_ids


def make_structured_node(
    name: str,
    *,
    deps: ModelDeps,
    prompt_version: str,
    meta: SchemaMeta,
    select_chunks: Callable[[AgentState], list[dict[str, Any]]],
    describe: Callable[[AgentState], str],
    require_chunks: bool = True,
    stream: bool = False,
) -> NodeFn:
    """Fenced evidence -> one structured call -> raw fields plus replay material. The model's
    output is re-validated against the schema; a mismatch (a stub, a provider quirk) is a
    guardrail event and the bounding step downstream rejects what it cannot use."""

    def node(state: AgentState) -> dict[str, Any]:
        chunks = select_chunks(state)
        if not chunks and require_chunks:
            return {"extracted_raw": {name: None}, "guardrail_events": [f"no_evidence:{name}"]}
        nonce = state.get("fence_nonce") or prompt_guard.new_nonce()
        framed, shown = frame_state_chunks(chunks, nonce)
        if not shown and require_chunks:
            return {
                "extracted_raw": {name: None},
                "guardrail_events": list(framed.events) + [f"no_evidence:{name}"],
                "fence_nonce": nonce,
            }
        prompt = load_prompt(prompt_version)
        system = [
            SystemBlock(prompt.text),
            SystemBlock(schema_block(meta.model), cache=True),
        ]
        evidence = framed.text if shown else "(no fenced evidence was gathered)\n"
        user = (
            f"{prompt_guard.fence_declaration(nonce)}\n\n{describe(state)}\n\n{evidence}\n"
            "Fill every field of the schema from the fenced evidence and the system data only."
        )
        extraction = deps.require().extract(
            schema=meta.model,
            system=system,
            user=user,
            model=deps.model,
            tracker=deps.tracker,
            reasoning=deps.reasoning,
            on_token=deps.stream if stream else None,
        )
        steps.note_model_call(
            extraction.model,
            input_tokens=extraction.usage.input_tokens,
            output_tokens=extraction.usage.output_tokens,
            cache_read=extraction.usage.cache_read_input_tokens,
            cache_write=extraction.usage.cache_creation_input_tokens,
            prompt_version=prompt_version,
        )
        fields = extraction.parsed.model_dump(mode="json")
        events = list(framed.events)
        try:
            meta.model.model_validate(fields)
        except ValidationError as exc:
            events.append(f"schema_validation_failed:{name}:{exc.error_count()}")
        return {
            "extracted_raw": {name: {"fields": fields, "chunk_ids": shown}},
            "requests": {
                name: {
                    "request": extraction.request,
                    "response": extraction.response,
                    "model": extraction.model,
                    "prompt_version": prompt_version,
                    "prompt_sha256": prompt.sha256,
                    "usage": extraction.usage.__dict__,
                }
            },
            "guardrail_events": events,
            "fence_nonce": nonce,
        }

    return node


def make_tool_loop(
    name: str,
    *,
    deps: ModelDeps,
    tools: dict[str, BoundTool],
    allowed: frozenset[str],
    prompt_version: str,
    goal: Callable[[AgentState], str],
    on_result: Callable[[AgentState, str, dict[str, Any]], dict[str, Any]],
    max_calls: int = 8,
) -> NodeFn:
    """The bounded tool loop. Off-list tool calls are answered with an error result and a
    guardrail event, never dispatched; every dispatched call counts against the budget."""
    offered = [tools[n].spec.schema() for n in sorted(tools) if n in allowed]

    def node(state: AgentState) -> dict[str, Any]:
        llm = deps.require()
        prompt = load_prompt(prompt_version)
        system = [SystemBlock(prompt.text, cache=True)]
        messages: list[dict[str, Any]] = [{"role": "user", "content": goal(state)}]
        updates: dict[str, Any] = {
            "messages": [],
            "tool_log": [],
            "guardrail_events": [],
            "retrieved_chunks": {},
            "evidence_coverage": {},
            "context": dict(state.get("context") or {}),
        }
        first_request: dict[str, Any] | None = None
        last_response: Any = None
        last_model: str | None = None
        calls_made = 0
        while calls_made < max_calls:
            turn = llm.chat(
                system=system,
                messages=messages,
                tools=offered,
                model=deps.model,
                tracker=deps.tracker,
                reasoning=deps.reasoning,
            )
            steps.note_model_call(
                turn.model,
                input_tokens=turn.usage.input_tokens,
                output_tokens=turn.usage.output_tokens,
                cache_read=turn.usage.cache_read_input_tokens,
                cache_write=turn.usage.cache_creation_input_tokens,
                prompt_version=prompt_version,
            )
            if first_request is None:
                first_request = dict(turn.request)
            last_response, last_model = turn.response, turn.model
            if turn.stop_reason not in ("tool_use", "end_turn", ""):
                updates["guardrail_events"].append(f"tool_loop_stopped:{name}:{turn.stop_reason}")
            messages.append({"role": "assistant", "content": turn.assistant_content})
            if not turn.tool_calls:
                break
            for call in turn.tool_calls:
                calls_made += 1
                deps.tracker.tool_call()
                if call.name not in tools or call.name not in allowed:
                    result = {"is_error": True, "error": "tool not permitted in this graph"}
                    updates["guardrail_events"].append(f"tool_not_permitted:{call.name}")
                else:
                    result = tools[call.name].call(call.arguments)
                    if result.get("redactions"):
                        updates["guardrail_events"].append(
                            f"secret_redacted_in_tool_result:{call.name}:{result['redactions']}"
                        )
                    if result.get("fixed_argument"):
                        updates["guardrail_events"].append(
                            f"tool_argument_fixed:{call.name}:{result['fixed_argument']}"
                        )
                    merged = on_result(state, call.name, result)
                    updates["retrieved_chunks"].update(merged.get("retrieved_chunks", {}))
                    updates["context"].update(merged.get("context", {}))
                    for k, v in merged.get("evidence_coverage", {}).items():
                        updates["evidence_coverage"][k] = (
                            updates["evidence_coverage"].get(k, False) or v
                        )
                updates["tool_log"].append(
                    {
                        "node": name,
                        "tool": call.name,
                        "arguments": call.arguments,
                        "is_error": result.get("is_error", False),
                    }
                )
                messages.append(tool_result_message(call.id, result))
                if calls_made >= max_calls:
                    updates["guardrail_events"].append(f"tool_loop_budget_exhausted:{name}")
                    break
        updates["messages"] = messages
        if first_request is not None:  # replay can check the gather prompt too
            updates["requests"] = {
                name: {
                    "request": first_request,
                    "response": last_response,
                    "model": last_model,
                    "prompt_version": prompt_version,
                    "prompt_sha256": prompt.sha256,
                }
            }
        return updates

    return node


def verify_named_items(
    items: list[dict[str, Any]], chunks: dict[str, dict[str, Any]], field_name: str
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """List-valued fields: each item is {name, citation}. Returns (verified names, citation
    records, events); an item whose citation fails is dropped."""
    names: list[str] = []
    records: list[dict[str, Any]] = []
    events: list[str] = []
    for item in items:
        cited = item.get("citation") or {}
        chunk_id = str(cited.get("chunk_id", ""))
        quote = str(cited.get("quote", ""))[: citations.MAX_QUOTE_CHARS]
        value = str(item.get("name", "")).strip()
        chunk = chunks.get(chunk_id)
        verified = False
        if not value:
            events.append(f"empty_item:{field_name}")
        elif chunk is None:
            events.append(f"citation_unknown_chunk:{field_name}:{chunk_id}")
        elif not citations.quote_supported(quote, str(chunk.get("text", ""))):
            events.append(f"citation_quote_not_found:{field_name}:{chunk_id}")
        elif not citations.value_in_quote(value, quote):
            events.append(f"citation_unsupported_value:{field_name}:{chunk_id}")
        else:
            verified = True
            names.append(value)
        records.append(
            {
                "chunk_id": chunk_id,
                "quote": quote,
                "supports": f"{field_name}:{value}",
                "verified": verified,
            }
        )
    return names, records, events


def make_verify_node(name: str, plan: dict[str, tuple[str, str, SchemaMeta]]) -> NodeFn:
    """plan: extraction node -> (list field on the schema, output key in `extracted`, meta).
    Scalar cited fields on the same schema go through the tier 2 verifier."""

    def node(state: AgentState) -> dict[str, Any]:
        chunks = state.get("retrieved_chunks", {})
        raw = state.get("extracted_raw", {})
        extracted: dict[str, Any] = {}
        records: list[dict[str, Any]] = []
        events: list[str] = []
        required_total = 0
        required_covered = 0
        given = 0
        verified_count = 0
        for node_name, (list_field, out_key, meta) in plan.items():
            payload = raw.get(node_name)
            required_total += 1
            if not payload:
                extracted[out_key] = None
                continue
            fields = payload["fields"]
            items = list(fields.get(list_field) or [])
            names, recs, evs = verify_named_items(items, chunks, list_field)
            given += len(recs)
            verified_count += sum(1 for r in recs if r["verified"])
            records.extend(recs)
            events.extend(evs)
            extracted[out_key] = names
            if names:
                required_covered += 1
            for scalar in meta.cited:  # cited scalars, e.g. review_date
                value = fields.get(scalar)
                cited = fields.get(f"{scalar}_citation")
                if cited is None:
                    extracted[scalar] = value if value in citations.ABSENT_VALUES else None
                    if value not in citations.ABSENT_VALUES:
                        events.append(f"uncited_field:{scalar}")
                    continue
                given += 1
                chunk = chunks.get(str(cited.get("chunk_id")))
                ok = (
                    chunk is not None
                    and citations.quote_supported(
                        str(cited.get("quote", "")), str(chunk.get("text", ""))
                    )
                    and (
                        scalar not in meta.value_in_quote
                        or citations.value_in_quote(value, str(cited.get("quote", "")))
                    )
                )
                records.append(
                    {
                        "chunk_id": str(cited.get("chunk_id")),
                        "quote": str(cited.get("quote", ""))[:300],
                        "supports": scalar,
                        "verified": ok,
                    }
                )
                if ok:
                    verified_count += 1
                    extracted[scalar] = value
                else:
                    events.append(f"citation_quote_not_found:{scalar}:{cited.get('chunk_id')}")
                    extracted[scalar] = None
        stats = {
            "coverage": required_covered / required_total if required_total else 1.0,
            "citation_rate": verified_count / given if given else 1.0,
        }
        return {
            "extracted": extracted,
            "verified_citations": records,
            "citation_stats": stats,
            "guardrail_events": events,
        }

    return node
