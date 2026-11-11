"""One allowlist per graph (spec.md Layer 8 "The graph registry and per-graph allowlists").
A graph can only ever see the tools on its list; the registry refuses anything else at
build time, and a model call naming an off-list tool is answered with an error result.
Each list is also its graph's base permit in `policy.check` (ccp/agent/authz.py, A-113).
This module names tools without importing the registry, so the policy module can read it."""

from __future__ import annotations

TIER3_BASE: tuple[str, ...] = (
    "search_evidence",
    "get_chunk",
    "list_evidence",
    "get_evidence_chunks",
    "find_exact",
    "outline",
    "read_span",
    "find_access",
    "list_assets",
    "get_person",
    "get_current_state",
)

# The Policy Agent reads policy documents only: each of these tools takes an evidence type,
# which its graph pins and its base rule requires (A-113, A-146).
POLICY_PINNED: tuple[str, ...] = ("search_evidence", "find_exact", "outline", "read_span")

ALLOWLISTS: dict[str, frozenset[str]] = {
    # an evaluator must not read prior verdicts, or it anchors on them
    "tier3": frozenset(TIER3_BASE),
    # root cause is per subject, not per framework
    "investigator": frozenset(
        TIER3_BASE + ("get_verdict", "list_findings", "get_access_grants", "get_control")
    ),
    # a task item reads evidence and the verdicts its facts name; it holds no list,
    # grant or write tool, and nothing that starts another run (A-151)
    "task_item": frozenset(
        ("search_evidence", "get_chunk", "list_evidence", "get_evidence_chunks")
        + ("find_exact", "outline", "read_span", "get_verdict", "get_control")
    ),
    # a policy draft must never contain named individuals' access details
    "policy": frozenset(POLICY_PINNED + ("get_chunk", "get_control", "get_framework_rollup")),
    # the copilot's restriction is that it has no write path, not fewer reads
    "copilot": frozenset(
        TIER3_BASE
        + (
            "get_verdict",
            "list_findings",
            "get_access_grants",
            "get_control",
            "get_framework_rollup",
            "get_investigation",
        )
    ),
}


class ToolNotAllowed(RuntimeError):
    pass


def check_allowlist(graph: str, tools: tuple[str, ...]) -> frozenset[str]:
    from ccp.agent.tools import TOOL_SPECS

    try:
        allowed = ALLOWLISTS[graph]
    except KeyError:
        raise ToolNotAllowed(f"unknown graph {graph!r}; known: {sorted(ALLOWLISTS)}") from None
    off_list = sorted(set(tools) - allowed)
    if off_list:
        raise ToolNotAllowed(f"graph {graph!r} may not bind {off_list}")
    unknown = sorted(set(tools) - set(TOOL_SPECS))
    if unknown:
        raise ToolNotAllowed(f"unknown tools {unknown}")
    return allowed
