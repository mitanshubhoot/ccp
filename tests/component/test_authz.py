"""MT3 (A-113): `policy.check` decides every tool call, denies by default, and logs each
decision.

The defects: a graph's tool list was the only control, checked when the graph was built; a
tool call carried no decision anyone could audit; a rule could not be watched before it took
effect; and a model that kept asking for a tool it may not call ran on until its call cap.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ccp.agent import authz, core
from ccp.agent.authz import Gate, Rule
from ccp.agent.tools import ToolContext, bind_tools
from ccp.config import Settings
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import StubLLMClient, SystemBlock, ToolCall
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

READS = {"control_id": "CTL-ACCESS-REVOKE"}


def test_nothing_is_permitted_without_a_rule() -> None:
    decision = authz.check("graph:nobody", "get_control", READS)
    assert decision.effect == "deny" and decision.rule_id is None
    assert "no rule permits" in decision.reason


def test_a_graph_may_call_only_its_own_tools() -> None:
    assert authz.check("graph:tier3", "search_evidence", {"evidence_type": "x"}).permitted
    assert not authz.check("graph:tier3", "get_verdict", {"verdict_id": "v"}).permitted
    offered = authz.offered("graph:tier3", ["get_verdict", "get_chunk", "list_findings"])
    assert offered == ["get_chunk"]  # denied tools leave the graph's list


def test_a_condition_on_arguments_cannot_be_widened_by_the_model() -> None:
    allowed = {"query": "q", "evidence_type": "policy_document"}
    other = {"query": "q", "evidence_type": "access_review_report"}
    assert authz.check("graph:policy", "search_evidence", allowed).permitted
    assert not authz.check("graph:policy", "search_evidence", other).permitted


def test_a_log_only_rule_records_what_it_would_permit(tmp_path: Path) -> None:
    rule = Rule(id="new-readers", principal="api:reader", tools=("get_control",), mode="log_only")
    table = authz.base_rules() + (rule,)
    watched = authz.check("api:reader", "get_control", READS, table)
    assert watched.effect == "deny" and watched.shadow_rule_id == "new-readers"
    switched = authz.base_rules() + (rule.model_copy(update={"mode": "enforce"}),)
    assert authz.check("api:reader", "get_control", READS, switched).permitted


def test_an_enforced_deny_outranks_a_permit_and_ask_holds_a_call() -> None:
    table = authz.base_rules() + (
        Rule(id="no-people", principal="graph:copilot", tools=("get_person",), effect="deny"),
        Rule(
            id="ask-grants", principal="graph:copilot", tools=("get_access_grants",), effect="ask"
        ),
    )
    person = authz.check("graph:copilot", "get_person", {"canonical_person_id": "p"}, table)
    assert person.effect == "deny" and person.rule_id == "no-people"
    grants = authz.check("graph:copilot", "get_access_grants", {"canonical_person_id": "p"}, table)
    assert grants.effect == "ask" and not grants.permitted


def test_a_broken_rule_file_denies_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "tool_policy.yaml"
    broken.write_text("rules: [{id: x, principal: 3, tools: nope", encoding="utf-8")
    monkeypatch.setattr(authz, "RULES_PATH", broken)
    decision = authz.check("graph:tier3", "get_chunk", {"chunk_id": "c"})
    assert decision.effect == "deny" and decision.reason.startswith("policy error")


# -- the gateway and the loop, against a database ---------------------------------------------


@pytest.fixture
def acme(base_settings: Settings) -> Iterator[ScopedDB]:
    try:
        with scratch_database(base_settings, prefix="ccp_authz") as settings:
            pool = app_pool(settings, max_size=2)
            try:
                yield ScopedDB(pool, TenantContext.for_id(settings, "acme"))
            finally:
                pool.close()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


def _ctx(db: ScopedDB) -> ToolContext:
    return ToolContext(db, HashingEmbeddingClient(), None, RetrievalConfig())


def _decisions(db: ScopedDB, run_id: str) -> list[tuple[str, str, str]]:
    return [
        (str(t), str(e), str(p))
        for t, e, p in db.fetch_all(
            "SELECT tool, effect, principal FROM policy_decisions WHERE run_id = %s"
            " ORDER BY decided_at, decision_id",
            (run_id,),
        )
    ]


@pytest.mark.db
def test_every_call_through_the_gateway_is_decided_and_logged(acme: ScopedDB) -> None:
    tools = bind_tools(
        _ctx(acme),
        ("get_control", "get_verdict"),
        gate=Gate("graph:investigator", run_id="acme:test:run-1", log_db=acme),
    )
    assert not tools["get_control"].call(READS)["is_error"]
    assert tools["get_control"].call({"control_id": 7})["is_error"]  # invalid: still logged
    assert _decisions(acme, "acme:test:run-1") == [
        ("get_control", "permit", "graph:investigator"),
        ("get_control", "deny", "graph:investigator"),
    ]


@pytest.mark.db
def test_three_denials_in_a_row_stop_the_run_for_review(acme: ScopedDB) -> None:
    def keeps_asking(
        system: list[SystemBlock], messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[str, list[ToolCall]]:
        n = len(messages)
        return "", [ToolCall(f"c{n}", "get_control", {"control_id": f"CTL-{n}"})]

    bound = bind_tools(
        _ctx(acme),
        ("get_control",),
        # tier 3 may not read the catalogue: offer it anyway, as a misconfigured graph would
        gate=Gate(
            "graph:tier3",
            run_id="acme:test:run-2",
            log_db=acme,
            table=authz.base_rules()
            + (
                Rule(
                    id="offer", principal="graph:tier3", tools=("get_control",), when={"x": ("y",)}
                ),
            ),
        ),
    )
    deps = core.ModelDeps(
        StubLLMClient(chat_handler=keeps_asking),
        "claude-haiku-4-5-20251001",
        BudgetTracker(
            RunBudget(max_tokens=100_000, max_usd=1.0, max_seconds=60, max_tool_calls=20)
        ),
    )
    node = core.make_tool_loop(
        "gather",
        deps=deps,
        tools=bound,
        allowed=frozenset(bound),
        prompt_version="gather_fallback_v1",
        goal=lambda state: "find the control",
        on_result=lambda state, tool, result: {},
        max_calls=8,
    )
    out = node({"thread_id": "acme:test:run-2"})  # type: ignore[typeddict-item]
    assert "tool_loop_stopped:gather:policy_denials" in out["guardrail_events"]
    assert [e for _, e, _ in _decisions(acme, "acme:test:run-2")] == ["deny", "deny", "deny"]
