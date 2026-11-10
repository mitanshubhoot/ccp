"""MT3 (A-114): the Rule of Two, at run time (the taint flag) and before it (the lint).

The defect: nothing tracked what a run had read, so a tool that changes state stayed
available after the run had read text a customer or a model wrote: a vendor PDF could steer
an agent holding private data into acting. Now the first such result taints the run's gate,
act-class tools are refused and leave the tool list, and propose-class tools stay; the lint
refuses a tool policy that hands an act tool to a principal that reads untrusted content and
private data.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from typer.testing import CliRunner

from ccp import cli
from ccp.agent import authz, core
from ccp.agent.authz import Gate, Rule
from ccp.agent.tools import TOOL_SPECS, BoundTool, GetControlArgs, ToolSpec
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import StubLLMClient, SystemBlock, ToolCall


class _Out(BaseModel):
    done: bool


READ = ToolSpec(
    "read_doc", "read a document", GetControlArgs, _Out, scopes=("evidence",), untrusted=True
)
ACT = ToolSpec(
    "revoke", "revoke an access grant", GetControlArgs, _Out, cls="act", scopes=("access",)
)
PROPOSE = ToolSpec(
    "propose_fix", "propose a fix", GetControlArgs, _Out, cls="propose", scopes=("access",)
)
EVERYTHING = (Rule(id="all", principal="graph:test", tools=("*",)),)


def _bound(gate: Gate) -> dict[str, BoundTool]:
    return {
        s.name: BoundTool(s, lambda ctx, args: {"done": True}, None, gate=gate)  # type: ignore[arg-type]
        for s in (READ, ACT, PROPOSE)
    }


def test_an_untrusted_read_taints_the_run_and_act_tools_go_off() -> None:
    gate = Gate("graph:test", table=EVERYTHING)
    tools = _bound(gate)
    assert tools["revoke"].call({"control_id": "c"})["is_error"] is False  # not yet tainted
    assert tools["read_doc"].call({"control_id": "c"})["is_error"] is False
    assert gate.tainted == "read_doc returned untrusted content"
    refused = tools["revoke"].call({"control_id": "c"})
    assert refused["is_error"] and refused["tainted"] and refused["denied"] == "taint"
    assert gate.decisions[-1][1].effect == "deny"
    assert "act tools are off" in gate.decisions[-1][1].reason
    assert tools["propose_fix"].call({"control_id": "c"})["is_error"] is False  # a person decides


def test_the_loop_stops_offering_act_tools_once_tainted() -> None:
    offered: list[list[str]] = []

    def model(
        system: list[SystemBlock], messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[str, list[ToolCall]]:
        offered.append(sorted(t["name"] for t in tools))
        if len(messages) == 1:
            return "", [ToolCall("c1", "read_doc", {"control_id": "c"})]
        return "done", []

    gate = Gate("graph:test", table=EVERYTHING)
    tools = _bound(gate)
    node = core.make_tool_loop(
        "gather",
        deps=core.ModelDeps(
            StubLLMClient(chat_handler=model),
            "claude-haiku-4-5-20251001",
            BudgetTracker(RunBudget(max_tokens=100_000, max_usd=1.0, max_seconds=60)),
        ),
        tools=tools,
        allowed=frozenset(tools),
        prompt_version="gather_fallback_v1",
        goal=lambda state: "read the document",
        on_result=lambda state, tool, result: {},
    )
    node({})  # type: ignore[typeddict-item]
    assert offered == [["propose_fix", "read_doc", "revoke"], ["propose_fix", "read_doc"]]


def test_the_lint_refuses_an_act_tool_beside_untrusted_and_private_reads() -> None:
    assert authz.rule_of_two(TOOL_SPECS) == []  # the policy as it stands
    specs = {**TOOL_SPECS, "revoke": ACT}
    table = authz.base_rules() + (
        Rule(id="copilot-acts", principal="graph:copilot", tools=("revoke",)),
    )
    problems = authz.rule_of_two(specs, table)
    assert len(problems) == 1 and problems[0].startswith("graph:copilot:")
    proposing = {**TOOL_SPECS, "revoke": ToolSpec(**{**ACT.__dict__, "cls": "propose"})}
    assert authz.rule_of_two(proposing, table) == []


def test_the_lint_command_passes_on_the_policy_as_it_stands() -> None:
    result = CliRunner().invoke(cli.app, ["agent", "lint"])
    assert result.exit_code == 0, result.output
    assert "rule of two" in result.output
