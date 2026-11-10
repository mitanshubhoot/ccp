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

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy
from pydantic import ValidationError

from ccp.agent.allowlists import check_allowlist
from ccp.agent.authz import DENIALS_STOP, Decision
from ccp.agent.bundle import bundle_sha256
from ccp.agent.runlog import RunLog
from ccp.agent.state import AgentState
from ccp.agent.tools import BoundTool
from ccp.evaluators import citations
from ccp.evaluators.extraction import SchemaMeta, schema_block
from ccp.ingest.consumers import RetryableError
from ccp.llm.budget import BudgetTracker
from ccp.llm.client import ChatTurn, Extraction, LLMClient, SystemBlock, tool_result_message
from ccp.llm.prompts import load_prompt
from ccp.observability import steps
from ccp.security import prompt_guard
from ccp.security.prompt_guard import FramedChunk
from ccp.storage.scoped import ScopedDB
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
    # The run log (A-109): with a database, every model call, tool call and fence nonce of a
    # run is recorded under the state's thread_id, and a resumed node is served what it
    # already did. `replay_of` names a recorded run to replay against its log alone.
    log_db: ScopedDB | None = None
    replay_of: str | None = None

    def require(self) -> LLMClient:
        if self.llm is None:
            raise RuntimeError("no model client configured for this node")
        return self.llm

    def runlog(self, state: AgentState) -> RunLog | None:
        thread_id = state.get("thread_id")
        if self.log_db is None or not thread_id:
            return None
        return RunLog(
            self.log_db,
            str(thread_id),
            source=self.replay_of,
            replay=self.replay_of is not None,
        )


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
    documents: Callable[[AgentState], list[str]] | None = None,
) -> NodeFn:
    """Fenced evidence -> one structured call -> raw fields plus replay material. The model's
    output is re-validated against the schema; a mismatch (a stub, a provider quirk) is a
    guardrail event and the bounding step downstream rejects what it cannot use. The evidence,
    and any further ``documents`` (tool output), go as document blocks; ``describe`` holds the
    instruction only (A-98)."""

    def node(state: AgentState) -> dict[str, Any]:
        chunks = select_chunks(state)
        if not chunks and require_chunks:
            return {"extracted_raw": {name: None}, "guardrail_events": [f"no_evidence:{name}"]}
        log = deps.runlog(state)
        nonce = state.get("fence_nonce") or (
            log.nonce(name) if log is not None else prompt_guard.new_nonce()
        )
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
            f"{prompt_guard.fence_declaration(nonce)}\n\n{describe(state)}\n\n"
            "Fill every field of the schema from the fenced evidence in the documents and the"
            " system data only."
        )
        docs = [evidence, *(documents(state) if documents else [])]

        def call() -> Extraction:
            return deps.require().extract(
                schema=meta.model,
                system=system,
                user=user,
                model=deps.model,
                tracker=deps.tracker,
                reasoning=deps.reasoning,
                on_token=deps.stream if stream else None,
                documents=docs,
            )

        if log is not None:  # recorded: a resumed or replayed node is served, not sampled
            digest = _extract_digest(meta.model, system, user, docs, deps.model, deps.reasoning)
            extraction, replayed = log.extract(name, digest, meta.model, call)
        else:
            extraction, replayed = call(), False
        if not replayed:
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
                    "bundle_sha256": bundle_sha256(),  # prompts, tools, errors (A-100)
                    "usage": extraction.usage.__dict__,
                }
            },
            "guardrail_events": events,
            "fence_nonce": nonce,
        }

    return node


class StopReason(StrEnum):
    """Why a tool loop ended (A-100). Anything but `normal` holds the outcome for review."""

    normal = "normal_end"
    call_cap = "call_cap"
    tool_errors = "tool_errors"
    refusal = "refusal"
    truncated = "truncated"
    loop_call = "loop_repeat_call"
    loop_result = "loop_repeat_result"
    policy_denials = "policy_denials"  # three calls in a row refused by policy.check (A-113)


LOOP_REPEATS = 3  # the same call, or the same result running, this many times is a loop
TOOL_ERROR_RUN = 3  # this many failed tool calls in a row end the loop


def model_stop(model_stop_reason: str, called_tools: bool) -> StopReason | None:
    """The model's own ending: a refusal, a truncated turn, or an answer with no tool call."""
    if model_stop_reason == "refusal":
        return StopReason.refusal
    if model_stop_reason in ("max_tokens", "model_context_window_exceeded"):
        return StopReason.truncated
    if not called_tools:
        return StopReason.normal
    return None


def stop_reason(
    history: list[tuple[str, str, str, bool]], calls_made: int, max_calls: int
) -> StopReason | None:
    """After a turn's tool calls: three failures in a row, the same tool and arguments three
    times, the same result three times running, or the call cap end the loop."""
    if len(history) >= TOOL_ERROR_RUN and all(h[3] for h in history[-TOOL_ERROR_RUN:]):
        return StopReason.tool_errors
    calls = [(h[0], h[1]) for h in history]
    if any(calls.count(c) >= LOOP_REPEATS for c in set(calls)):
        return StopReason.loop_call
    recent = [h[2] for h in history[-LOOP_REPEATS:]]
    if len(recent) == LOOP_REPEATS and len(set(recent)) == 1:
        return StopReason.loop_result
    if calls_made >= max_calls:
        return StopReason.call_cap
    return None


def _request_digest(
    system: list[SystemBlock],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str = "",
) -> str:
    material = json.dumps(
        {"system": [b.text for b in system], "messages": messages, "tools": tools, "model": model},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _extract_digest(
    schema: type[Any],
    system: list[SystemBlock],
    user: str,
    documents: list[str],
    model: str,
    reasoning: bool,
) -> str:
    """What a structured call sends, as one hash: the run log's key for it (A-109)."""
    material = json.dumps(
        {
            "schema": f"{schema.__module__}.{schema.__qualname__}",
            "system": [b.text for b in system],
            "user": user,
            "documents": documents,
            "model": model,
            "reasoning": reasoning,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


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
    guardrail event, never dispatched; every dispatched call counts against the budget. The
    loop ends only through `stop_reason` (A-100), and any stop but a normal end is a
    `tool_loop_stopped:<node>:<reason>` event that holds the outcome for review. Each model
    turn is its own step; a retried node replays the turns it already took (`memo`) instead
    of sampling the model again. With a run log (A-109) every turn, tool result and the
    fence nonce are recorded as they happen, so a node restarted by a resume, in a new
    process, is served all of them and its requests come out the same."""
    build_nonce = prompt_guard.new_nonce()  # without a run log: one fence per graph build

    def offered_now(gate: Any) -> list[dict[str, Any]]:
        """The tools offered this turn: act-class tools leave once the run is tainted."""
        tainted = gate is not None and getattr(gate, "tainted", None) is not None
        return [
            tools[n].spec.schema()
            for n in sorted(tools)
            if n in allowed and not (tainted and getattr(tools[n].spec, "cls", "read") == "act")
        ]

    memo: dict[str, Any] = {}  # request digest -> ChatTurn, for this graph build

    def node(state: AgentState) -> dict[str, Any]:
        llm = deps.require()
        log = deps.runlog(state)
        nonce = log.nonce(name) if log is not None else build_nonce
        prompt = load_prompt(prompt_version)
        system = [SystemBlock(prompt.text, cache=True)]
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": f"{goal(state)}\n\n{prompt_guard.tool_fence_declaration(nonce)}",
            }
        ]
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
        history: list[tuple[str, str, str, bool]] = []  # (tool, arguments, result digest, error)
        reason: StopReason | None = None
        turn_no = 0
        denied_run = 0  # calls refused by policy.check in a row (A-113)
        gate = next(
            (g for g in (getattr(t, "gate", None) for t in tools.values()) if g is not None), None
        )
        while reason is None:
            turn_no += 1
            offered = offered_now(gate)
            digest = _request_digest(system, messages, offered, deps.model)

            def sample(this_turn: list[dict[str, Any]] = offered) -> ChatTurn:
                return llm.chat(
                    system=system,
                    messages=messages,
                    tools=this_turn,
                    model=deps.model,
                    tracker=deps.tracker,
                    reasoning=deps.reasoning,
                )

            with steps.step(
                f"{name}.turn",
                layer="agent",
                kind="generation",
                inputs={"turn": turn_no, "replayed": digest in memo},
            ) as turn_step:
                if digest in memo:  # a retried node: this turn was already taken
                    turn, served = memo[digest], True
                elif log is not None:  # a resumed or replayed node: the recorded turn
                    turn, served = log.chat(name, digest, sample)
                    if served:
                        turn_step.event("served_from_run_log")
                else:
                    turn, served = sample(), False
                memo[digest] = turn
                if not served:
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
            messages.append({"role": "assistant", "content": turn.assistant_content})
            reason = model_stop(turn.stop_reason, bool(turn.tool_calls))
            if reason is not None:
                break
            for call in turn.tool_calls:
                calls_made += 1
                deps.tracker.tool_call()
                if call.name not in tools or call.name not in allowed:
                    result = {
                        "is_error": True,
                        "error": "tool not permitted in this graph",
                        "denied": "not_on_list",
                    }
                    updates["guardrail_events"].append(f"tool_not_permitted:{call.name}")
                    if gate is not None:  # refused before the gateway: decided and logged here
                        gate.record(
                            call.name,
                            "unknown",
                            call.arguments,
                            Decision("deny", None, "not on this graph's tool list"),
                        )
                else:
                    bound = tools[call.name]
                    if log is not None:  # recorded, so a resumed loop sees the same result
                        # keyed by the turn that asked and the call: ids alone repeat across
                        # turns and conversations
                        result = log.tool(
                            name,
                            f"{digest}:{call.id}",
                            call.name,
                            call.arguments,
                            lambda b=bound, c=call: b.call(c.arguments),  # type: ignore[misc]
                        )
                    else:
                        result = bound.call(call.arguments)
                    if result.get("redactions"):
                        updates["guardrail_events"].append(
                            f"secret_redacted_in_tool_result:{call.name}:{result['redactions']}"
                        )
                    for signal in ("malformed_result", "rate_limited", "timeout_exceeded"):
                        if result.get(signal):  # the gateway's own findings (A-112)
                            updates["guardrail_events"].append(f"tool_{signal}:{call.name}")
                    if result.get("missing"):
                        updates["guardrail_events"].append(
                            f"tool_data_missing:{call.name}:{result['missing']}"
                        )
                    if result.get("fixed_argument"):
                        updates["guardrail_events"].append(
                            f"tool_argument_fixed:{call.name}:{result['fixed_argument']}"
                        )
                    if result.get("policy"):
                        updates["guardrail_events"].append(
                            f"tool_policy_{result['policy']}:{call.name}:{result.get('denied')}"
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
                guarded = prompt_guard.guard_tool_result(
                    call.name, json.dumps(result, default=str, ensure_ascii=False), nonce
                )
                updates["guardrail_events"].extend(
                    e for e in guarded.events if not e.startswith("secret_redacted_in_tool_result")
                )  # the tool seam already reported its own redactions above
                messages.append(tool_result_message(call.id, result, guarded.content))
                history.append(
                    (
                        call.name,
                        json.dumps(call.arguments, sort_keys=True, default=str),
                        hashlib.sha256(guarded.content.split("\n", 1)[-1].encode()).hexdigest(),
                        bool(result.get("is_error")),
                    )
                )
                denied_run = denied_run + 1 if result.get("denied") else 0
                if denied_run >= DENIALS_STOP or calls_made >= max_calls:
                    break
            if denied_run >= DENIALS_STOP:  # a model that keeps asking for what it may not have
                reason = StopReason.policy_denials
                break
            reason = stop_reason(history, calls_made, max_calls)
        if reason is not StopReason.normal:
            updates["guardrail_events"].append(f"tool_loop_stopped:{name}:{reason.value}")
            if reason is StopReason.call_cap:  # the older name, kept for readers of the log
                updates["guardrail_events"].append(f"tool_loop_budget_exhausted:{name}")
        updates["messages"] = messages
        if first_request is not None:  # replay can check the gather prompt too
            updates["requests"] = {
                name: {
                    "request": first_request,
                    "response": last_response,
                    "model": last_model,
                    "prompt_version": prompt_version,
                    "prompt_sha256": prompt.sha256,
                    "bundle_sha256": bundle_sha256(),  # prompts, tools, errors (A-100)
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
                    and citations.check_value(scalar, value, str(cited.get("quote", "")), meta)
                    is None  # the tier 2 rules: every deciding field is checked (A-98)
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
