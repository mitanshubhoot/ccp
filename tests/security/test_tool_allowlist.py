"""S4: tool allowlist per graph; S5: tools cannot be pointed at another tenant."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from ccp.agent import core
from ccp.agent.allowlists import ALLOWLISTS, ToolNotAllowed, check_allowlist
from ccp.agent.core import ModelDeps, NodePolicies
from ccp.agent.tools import TOOL_SPECS, BoundTool, SearchEvidenceArgs, ToolSpec
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import StubLLMClient
from tests.support.stub_agent import stub_chat_off_list_tool

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("graph", sorted(ALLOWLISTS))
def test_every_graph_rejects_an_off_list_tool_at_build_time(graph: str) -> None:  # S4 (a)
    allowed = ALLOWLISTS[graph]
    with pytest.raises(ToolNotAllowed):
        check_allowlist(graph, tuple(allowed) + ("send_email",))
    assert check_allowlist(graph, tuple(allowed)) == allowed
    off_list = sorted(set(TOOL_SPECS) - allowed)
    for name in off_list:
        with pytest.raises(ToolNotAllowed):
            check_allowlist(graph, (name,))


def test_tier3_and_policy_lists_exclude_what_the_spec_says() -> None:
    assert not {"get_verdict", "list_findings"} & ALLOWLISTS["tier3"]
    assert not {"get_person", "get_access_grants"} & ALLOWLISTS["policy"]
    assert "get_framework_rollup" not in ALLOWLISTS["investigator"]
    assert ALLOWLISTS["copilot"] == frozenset(TOOL_SPECS)


def test_a_hallucinated_tool_call_is_refused_and_never_dispatched() -> None:  # S4 (b)
    fired: list[str] = []

    def sentinel(ctx: object, args: object) -> dict[str, str]:
        fired.append("side effect")
        return {"ok": "yes"}

    spec = ToolSpec("search_evidence", "x", SearchEvidenceArgs)
    tools = {"search_evidence": BoundTool(spec, sentinel, None)}  # type: ignore[arg-type]
    deps = ModelDeps(
        StubLLMClient(chat_handler=stub_chat_off_list_tool),
        "m",
        BudgetTracker(RunBudget(max_tokens=10_000, max_usd=1, max_seconds=30)),
    )
    loop = core.make_tool_loop(
        "gather",
        deps=deps,
        tools=tools,
        allowed=ALLOWLISTS["tier3"],
        prompt_version="gather_fallback_v1",
        goal=lambda s: "find it",
        on_result=lambda s, n, r: {},
    )
    out = loop({"retrieved_chunks": {}, "evidence_coverage": {}})
    assert "tool_not_permitted:delete_repo" in out["guardrail_events"]
    assert out["tool_log"][0]["is_error"] is True and fired == []
    tool_result = out["messages"][2]["content"][0]
    assert tool_result["is_error"] is True and "not permitted" in tool_result["content"]


def test_tools_import_nothing_with_side_effects_and_open_no_write_path() -> None:  # S4 (c)
    source = (REPO_ROOT / "ccp" / "agent" / "tools.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    forbidden = (
        "ccp.remediation",
        "ccp.approvals",
        "ccp.policy",
        "ccp.investigation",
        "ccp.ingest",
    )
    assert not [m for m in imported if m.startswith(forbidden)]
    lowered = source.lower()
    for verb in ("insert into", "update ", "delete from", "db.execute(", ".transaction("):
        assert verb not in lowered, f"tools.py contains a write path: {verb!r}"


def test_no_tool_accepts_a_tenant_id_and_extra_fields_are_rejected() -> None:  # S5
    for spec in TOOL_SPECS.values():
        assert "tenant_id" not in spec.args.model_fields, spec.name
        assert spec.args.model_config.get("extra") == "forbid", spec.name
    with pytest.raises(ValidationError):
        SearchEvidenceArgs(query="x", evidence_type="policy_document", tenant_id="globex")  # type: ignore[call-arg]
    tool = BoundTool(TOOL_SPECS["search_evidence"], lambda c, a: {"ok": True}, None)  # type: ignore[arg-type]
    refused = tool.call({"query": "x", "evidence_type": "policy_document", "tenant_id": "globex"})
    assert refused["is_error"] is True and "invalid arguments" in refused["error"]


def test_a_node_without_policies_cannot_be_built() -> None:
    spec = core.GraphSpec(
        name="tier3",
        tools=(),
        nodes=(core.NodeSpec("n", lambda s: {}),),
        edges=((core.START, "n"), ("n", core.END)),
    )
    with pytest.raises(ValueError, match="without policies"):
        spec.validate()
    ok = core.GraphSpec(
        name="tier3",
        tools=(),
        nodes=(core.NodeSpec("n", lambda s: {}, NodePolicies.default()),),
        edges=((core.START, "n"), ("n", core.END)),
    )
    ok.validate()
    with pytest.raises(ToolNotAllowed):
        core.GraphSpec(name="nope", tools=(), nodes=()).validate()
