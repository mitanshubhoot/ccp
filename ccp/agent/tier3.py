"""The tier 3 wrapper (spec.md Layer 7 "Tier 3", Layer 8): builds the workflow graph for one
tenant, runs it under a thread id of ``{tenant}:{test}:{subject}:{run_id}``, and converts the
final state to a Verdict. When the graph stops at the gate the wrapper returns a real
needs_review verdict at once, records the thread as interrupted, opens an approval item, and a
separate process resumes it later from the decision recorded on that item (A-95).

    evaluate(...)  ──► the failed thread for this key, or a new one ──► stream ──► get_state
                                     ├─ interrupted ──► needs_review verdict, approval item opened
                                     └─ finished    ──► Verdict from state, thread finished
    resume(...)    ──► recorded decision, unchanged payload ──► Command(resume=...) ──► Verdict
                                     └─ regather back to the gate ──► a new approval item
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from ulid import ULID

from ccp.agent import core, threads
from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.graphs import tier3 as graph_module
from ccp.agent.tools import ToolContext, bind_tools
from ccp.approvals import store as approvals
from ccp.config import Settings
from ccp.evaluators import budget_gate, registry
from ccp.evaluators.context import EvaluationContext, load_context
from ccp.evaluators.tier2 import AITierUnavailable, Providers
from ccp.evaluators.verdict import TriggerReason, Verdict, evaluation_key
from ccp.knowledge import graph as knowledge
from ccp.knowledge.graph import TestSpec
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.observability import steps
from ccp.retrieval.chunker import CHUNKER_VERSION
from ccp.storage import checkpoints
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from ccp.types import VerdictStatus

GRAPH = graph_module.GRAPH_NAME
APPROVAL_KIND: approvals.Kind = "tier3_interrupt"


class ThreadTenantMismatch(RuntimeError):
    """A thread id from another tenant was presented; refused before any saver access."""


class PayloadChanged(RuntimeError):
    """The thread's pending interrupt is not the one the reviewer was shown."""


def _check_tenant(db: ScopedDB, thread_id: str) -> None:
    if checkpoints.tenant_of_thread(thread_id) != db.tenant.tenant_id:
        raise ThreadTenantMismatch(
            f"thread {thread_id!r} does not belong to tenant {db.tenant.tenant_id!r}"
        )


def _build(
    db: ScopedDB, settings: Settings, spec: TestSpec, providers: Providers, now: datetime
) -> tuple[Any, PostgresSaver, Any, BudgetTracker]:
    tracker = BudgetTracker(RunBudget.from_settings(settings))
    tools = bind_tools(
        ToolContext(db, providers.embedder, providers.reranker, providers.retrieval),
        tuple(sorted(ALLOWLISTS[GRAPH])),
    )
    deps = core.ModelDeps(providers.llm, settings.ccp_tier3_model, tracker, reasoning=True)
    graph_spec = graph_module.build_spec(db.tenant, spec, tools, deps, now=now)
    pool = checkpoints.saver_pool(settings, db.tenant)
    saver = PostgresSaver(cast(Any, pool))
    return core.build_graph(graph_spec, saver), saver, pool, tracker


def _run(
    graph: Any, payload: Any, config: dict[str, Any], into: list[str] | None = None
) -> list[str]:
    """Stream the graph, appending each step to ``into`` as it completes, so a run that
    fails part way still records how far it got."""
    trajectory = into if into is not None else []
    for update in graph.stream(payload, config, stream_mode="updates"):
        for key in update:
            trajectory.append("interrupt" if key == "__interrupt__" else str(key))
    return trajectory


def _interrupt_payload(snapshot: Any) -> dict[str, Any] | None:
    for item in getattr(snapshot, "interrupts", ()) or ():
        return dict(getattr(item, "value", {}) or {})
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            return dict(getattr(item, "value", {}) or {})
    return None


def item_payload(thread_id: str, interrupt: dict[str, Any]) -> dict[str, Any]:
    """What the approval item shows the reviewer; its hash binds the decision to it."""
    return {"thread_id": thread_id, **interrupt}


def _interrupted_verdict(
    tenant: TenantContext,
    spec: TestSpec,
    subject_key: str,
    payload: dict[str, Any],
    thread_id: str,
    model: str,
    trigger: datetime,
    reason: TriggerReason,
    now: datetime,
    state: dict[str, Any],
) -> Verdict:
    draft = dict(payload.get("draft") or {})
    reasons = list(draft.get("reasons") or ["stopped at the human gate"])
    return Verdict(
        evaluation_key=evaluation_key(
            tenant.tenant_id, spec.test_id, subject_key, trigger, spec.prompt_version, model
        ),
        tenant_id=tenant.tenant_id,
        test_id=spec.test_id,
        subject_key=subject_key,
        status=VerdictStatus.needs_review,
        reasoning="; ".join(reasons) + f"; awaiting human review (thread {thread_id})",
        citations=tuple(
            {
                "chunk_id": c["chunk_id"],
                "quote": c["quote"],
                "supports": c["supports"],
                "verified": c["verified"],
            }
            for c in payload.get("citations") or []
        ),
        evidence_refs=tuple(
            sorted(
                {
                    str(c.get("evidence_entity_id"))
                    for c in state.get("retrieved_chunks", {}).values()
                }
            )
        ),
        confidence=float(draft.get("confidence", 0.0)),
        confidence_cap=float(draft.get("confidence_cap", 1.0)),
        tier=3,
        model=model,
        prompt_version=spec.prompt_version,
        retrieved_chunk_ids=tuple(sorted(state.get("retrieved_chunks", {}))),
        guardrail_events=tuple(
            list(payload.get("guardrail_events") or []) + [f"interrupted_for_review:{thread_id}"]
        ),
        extracted_fields=dict(state.get("extracted") or {}),
        chunker_version=CHUNKER_VERSION,
        schema_version="hr4_fields_v1",
        fence_nonce=state.get("fence_nonce"),
        trigger_observed_at=trigger,
        trigger_reason=reason,
        valid_from=trigger,
        request_json={"thread_id": thread_id, "calls": state.get("requests", {})},
        response_json={"interrupt": payload},
        created_at=now,
    )


def evaluate(
    db: ScopedDB,
    settings: Settings,
    ctx: EvaluationContext,
    spec: TestSpec,
    subject: str,
    providers: Providers,
    *,
    reason: TriggerReason = "impact",
) -> Verdict:
    if providers.llm is None:
        raise AITierUnavailable("ANTHROPIC_API_KEY is not set; tier 3 evaluation is unavailable")
    tenant = db.tenant
    subject_key = f"{spec.test_id}|{subject}"
    key = evaluation_key(
        tenant.tenant_id,
        spec.test_id,
        subject_key,
        ctx.trigger_observed_at,
        spec.prompt_version,
        settings.ccp_tier3_model,
    )
    previous = threads.resumable(db, key)
    if previous is not None:  # a retry: continue that thread from its last checkpoint
        thread_id = previous.thread_id
        threads.mark_running(db, thread_id)
    else:
        run_id = str(ULID())
        thread_id = checkpoints.thread_id_for(tenant, spec.test_id, subject_key, run_id)
        threads.create(db, thread_id, GRAPH, spec.test_id, subject_key, evaluation_key=key)
    graph, _saver, pool, tracker = _build(db, settings, spec, providers, ctx.now)
    config = {"configurable": {"thread_id": thread_id}}
    initial: dict[str, Any] = {
        "tenant_id": tenant.tenant_id,
        "test_id": spec.test_id,
        "subject_key": subject_key,
        "required_evidence_types": [e.value for e in spec.evidence_types],
        "thread_id": thread_id,
        "trigger_observed_at": ctx.trigger_observed_at.isoformat(),
        "trigger_reason": reason,
        "now": ctx.now.isoformat(),
        "model": settings.ccp_tier3_model,
        "retrieved_chunks": {},
        "evidence_coverage": {},
        "extracted_raw": {},
        "extracted": {},
        "requests": {},
    }
    trajectory: list[str] = list(previous.trajectory) if previous is not None else []
    try:
        with steps.step(
            "tier3.run",
            layer="agent",
            tenant=tenant,
            inputs={"test_id": spec.test_id, "subject": subject, "thread_id": thread_id},
        ) as s:
            checkpointed = previous is not None and bool(graph.get_state(config).values)
            if checkpointed:
                s.event(f"resumed_thread:{thread_id}")
                _run(graph, None, config, trajectory)  # only the steps not yet done run
            else:
                trajectory.clear()
                _run(graph, initial, config, trajectory)
            budget_gate.record_spend(db, tracker.usd, tracker.tokens, s.step_id, ctx.now)
            snapshot = graph.get_state(config)
            payload = _interrupt_payload(snapshot)
            values = dict(snapshot.values or {})
            s.set_output({"trajectory": trajectory, "interrupted": payload is not None})
            if payload is not None:
                verdict = _interrupted_verdict(
                    tenant,
                    spec,
                    subject_key,
                    payload,
                    thread_id,
                    settings.ccp_tier3_model,
                    ctx.trigger_observed_at,
                    reason,
                    ctx.now,
                    values,
                )
                threads.mark_interrupted(db, thread_id, payload, trajectory)
                approvals.open_item(db, APPROVAL_KIND, thread_id, item_payload(thread_id, payload))
                return verdict
            final = values.get("final_verdict")
            if not final:
                raise RuntimeError("graph finished without a final verdict")
            verdict = Verdict.model_validate(final)
            threads.mark_finished(db, thread_id, verdict.verdict_id, trajectory)
            return verdict
    except Exception as exc:
        threads.mark_failed(db, thread_id, f"{type(exc).__name__}: {exc}", trajectory)
        raise
    finally:
        pool.close()


def _recorded_decision(db: ScopedDB, thread_id: str, approval_id: str) -> approvals.Approval:
    """The named approval item, which must be this thread's and hold a recorded decision."""
    item = approvals.get(db, approval_id)
    if item is None or item.kind != APPROVAL_KIND or item.ref_id != thread_id:
        raise LookupError(f"approval {approval_id!r} is not an item of thread {thread_id!r}")
    if not item.is_decided or item.decision is None:
        raise LookupError(
            f"thread {thread_id!r} has no recorded reviewer decision on {approval_id!r}; decide"
            " it with `ccp approvals approve` or `ccp approvals reject`"
        )
    return item


def pending_payload_sha256(
    db: ScopedDB, settings: Settings, thread_id: str, providers: Providers | None = None
) -> str | None:
    """The hash of the item payload for the interrupt the thread is waiting on, or None."""
    _check_tenant(db, thread_id)
    row = threads.get(db, thread_id)
    if row is None or not row.test_id:
        return None
    spec = knowledge.load().tests[row.test_id]
    active = providers or Providers.from_settings(settings)
    graph, _saver, pool, _tracker = _build(db, settings, spec, active, datetime.now(UTC))
    try:
        waiting = _interrupt_payload(graph.get_state({"configurable": {"thread_id": thread_id}}))
    finally:
        pool.close()
    return None if waiting is None else approvals.payload_hash(item_payload(thread_id, waiting))


def resume(
    db: ScopedDB,
    settings: Settings,
    thread_id: str,
    providers: Providers | None = None,
    now: datetime | None = None,
    *,
    approval_id: str,
    retry: bool = False,
) -> Verdict:
    """Continue an interrupted thread with the decision recorded on approval ``approval_id``;
    there is no way to pass a decision in, and the item's payload must be the interrupt the
    thread is waiting on. The decision becomes the trigger of the resulting verdict (reason
    human_decision), so its key never collides with the interrupted verdict's. ``retry``
    also picks up a thread whose earlier resume failed, or one that finished but whose
    verdict was not stored (the final state is read back)."""
    _check_tenant(db, thread_id)
    row = threads.get(db, thread_id)
    allowed = ("interrupted", "failed", "finished") if retry else ("interrupted",)
    if row is None or row.status not in allowed:
        raise LookupError(f"thread {thread_id!r} is not waiting for a decision")
    assert row.test_id and row.subject_key
    item = _recorded_decision(db, thread_id, approval_id)
    decision = dict(item.decision or {})
    spec = knowledge.load().tests[row.test_id]
    moment = now or datetime.now(UTC)
    active = providers or Providers.from_settings(settings)
    graph, _saver, pool, tracker = _build(db, settings, spec, active, moment)
    config = {"configurable": {"thread_id": thread_id}}
    trajectory = list(row.trajectory)
    try:
        with steps.step(
            "tier3.resume",
            layer="agent",
            tenant=db.tenant,
            inputs={
                "thread_id": thread_id,
                "decision": decision.get("action"),
                "approval_id": item.approval_id,
                "retry": retry,
            },
        ) as s:
            before = graph.get_state(config)
            waiting = _interrupt_payload(before)
            if waiting is not None:
                shown = approvals.payload_hash(item_payload(thread_id, waiting))
                if shown != item.payload_sha256:
                    raise PayloadChanged(
                        f"thread {thread_id!r} is waiting on a payload that approval"
                        f" {item.approval_id!r} did not show"
                    )
                command: Command[Any] = Command(
                    update={
                        "trigger_observed_at": moment.isoformat(),
                        "trigger_reason": "human_decision",
                        "now": moment.isoformat(),
                    },
                    resume=decision,
                )
                trajectory += _run(graph, command, config)
            elif not retry:
                raise LookupError(f"thread {thread_id!r} is not waiting at the gate")
            else:  # past the gate already: it must have applied exactly this decision
                if dict(before.values or {}).get("human_decision") != decision:
                    raise PayloadChanged(
                        f"thread {thread_id!r} went past the gate with a decision other than"
                        f" the one recorded on {item.approval_id!r}"
                    )
                if before.next:  # a node after the gate failed; finish the run
                    trajectory += _run(graph, None, config)
            budget_gate.record_spend(db, tracker.usd, tracker.tokens, s.step_id, moment)
            snapshot = graph.get_state(config)
            payload = _interrupt_payload(snapshot)
            values = dict(snapshot.values or {})
            s.set_output({"trajectory": trajectory, "interrupted": payload is not None})
            if payload is not None:  # regather led back to the gate: a new decision is needed
                threads.mark_interrupted(db, thread_id, payload, trajectory)
                approvals.open_item(db, APPROVAL_KIND, thread_id, item_payload(thread_id, payload))
                return _interrupted_verdict(
                    db.tenant,
                    spec,
                    row.subject_key,
                    payload,
                    thread_id,
                    settings.ccp_tier3_model,
                    moment,
                    "human_decision",
                    moment,
                    values,
                )
            final = values.get("final_verdict")
            if not final:
                raise RuntimeError("graph finished without a final verdict")
            verdict = Verdict.model_validate(final)
            threads.mark_finished(db, thread_id, verdict.verdict_id, trajectory, decision)
            return verdict
    except Exception as exc:
        threads.mark_failed(db, thread_id, f"{type(exc).__name__}: {exc}", trajectory)
        raise
    finally:
        pool.close()


def evaluate_subject(
    db: ScopedDB,
    settings: Settings,
    spec: TestSpec,
    subject: str,
    providers: Providers,
    trigger: datetime,
    now: datetime | None = None,
    reason: TriggerReason = "impact",
) -> Verdict:
    ctx = load_context(db, settings, spec, trigger, now, reason=reason)
    return evaluate(db, settings, ctx, spec, subject, providers, reason=reason)


registry.register_tier(3, evaluate)
