"""Graph state with reducers (spec.md Layer 8). Every multi-writer field declares how two
writes merge; single-writer fields still declare keep_last so a future second writer cannot
silently clobber. Values are JSON-native so any checkpointer can serialize them."""

from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict


def merge_chunks(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}  # keyed by chunk_id; a re-retrieval is idempotent


def or_coverage(left: dict[str, bool], right: dict[str, bool]) -> dict[str, bool]:
    return {k: left.get(k, False) or right.get(k, False) for k in set(left) | set(right)}


def keep_last(left: Any, right: Any) -> Any:
    return right if right is not None else left


def merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}


class AgentState(TypedDict, total=False):
    # inputs, written once by the wrapper
    tenant_id: str
    test_id: str
    subject_key: str
    required_evidence_types: list[str]
    thread_id: str
    verdict_id: str
    # multi-writer fields, every one has a reducer
    messages: Annotated[list[dict[str, Any]], add]
    retrieved_chunks: Annotated[dict[str, dict[str, Any]], merge_chunks]
    evidence_coverage: Annotated[dict[str, bool], or_coverage]
    tool_log: Annotated[list[dict[str, Any]], add]
    guardrail_events: Annotated[list[str], add]
    extracted_raw: Annotated[dict[str, Any], merge_dicts]  # per extraction node: parsed fields
    extracted: Annotated[dict[str, Any], merge_dicts]  # verified values
    requests: Annotated[dict[str, Any], merge_dicts]  # per model call: request and response
    fallback_used: Annotated[list[str], add]
    # single-writer fields
    trigger_observed_at: Annotated[str | None, keep_last]
    trigger_reason: Annotated[str | None, keep_last]
    now: Annotated[str | None, keep_last]
    model: Annotated[str | None, keep_last]
    plan: Annotated[str | None, keep_last]
    fallback_target: Annotated[str | None, keep_last]
    fence_nonce: Annotated[str | None, keep_last]
    verified_citations: Annotated[list[dict[str, Any]] | None, keep_last]
    citation_stats: Annotated[dict[str, float] | None, keep_last]
    draft_verdict: Annotated[dict[str, Any] | None, keep_last]
    confidence_cap: Annotated[float | None, keep_last]
    human_decision: Annotated[dict[str, Any] | None, keep_last]
    context: Annotated[dict[str, Any] | None, keep_last]  # investigator: what is known
    mode: Annotated[str | None, keep_last]  # policy: gap | draft
    question: Annotated[str | None, keep_last]  # copilot: the operator's question this turn
    intent: Annotated[str | None, keep_last]  # copilot: explain | status | missing | act | other
    turn: Annotated[int | None, keep_last]  # copilot: 1-based turn within the session thread
    final: Annotated[dict[str, Any] | None, keep_last]  # non-verdict graphs' output
    final_verdict: Annotated[dict[str, Any] | None, keep_last]
