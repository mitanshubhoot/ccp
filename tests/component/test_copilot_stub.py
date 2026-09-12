"""The Copilot with a stub model: the five intent contracts, the status answer that never says
"ready", requested actions that open nothing until confirmed, and one thread per session."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from ccp.agent import threads
from ccp.agent import tier3 as _tier3  # noqa: F401 - registers tier 3
from ccp.agent.graphs.copilot import TRAJECTORY, says_forbidden
from ccp.approvals import store as approvals
from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.copilot import runner, schema
from ccp.evaluators import budget_gate
from ccp.evaluators import runner as evaluations
from ccp.evaluators.tier2 import Providers
from ccp.investigation import schema as investigations
from ccp.llm.client import StubLLMClient
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants
from tests.support.stub_agent import stub_chat_search_once, stub_extract_agent
from tests.support.stub_copilot import answer, classify, gather
from tests.support.stub_investigator import diagnose, gather_once

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


class Stub(StubLLMClient):
    """Routes extraction by schema (intent, answer, investigation, tier schemas) and chat by
    the prompt (copilot gatherer, investigator gatherer, tier 3 fallback searcher)."""

    def __init__(self) -> None:
        super().__init__()

        def extract(schema, system, user):  # type: ignore[no-untyped-def]
            name = schema.__name__
            if name == "IntentDraft":
                return classify(schema, system, user)
            if name == "CopilotAnswerDraft":
                return answer(schema, system, user)
            if name == "InvestigationDraft":
                return diagnose(schema, system, user)
            return stub_extract_agent(schema, system, user)

        def chat(system, messages, tools):  # type: ignore[no-untyped-def]
            text = system[0].text.lower()
            if "copilot" in text:
                return gather(system, messages, tools)
            if "investigator" in text:
                return gather_once(system, messages, tools)
            return stub_chat_search_once(system, messages, tools)

        self.handler = extract
        self.chat_handler = chat


def make_providers() -> Providers:
    return Providers(
        embedder=HashingEmbeddingClient(), reranker=None, llm=Stub(), retrieval=RetrievalConfig()
    )


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, Providers]]:
    with prepared_tenants(base_settings, "ccp_copilot", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = make_providers()
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1", "GH-4"))
        evaluations.drain(acme, settings, now=NOW, providers=providers)
        state_writer.drain(acme)
        yield settings, acme, providers


def _tools(turn: runner.Turn) -> list[str]:
    return [str(t.get("tool")) for t in (turn.answer.request_json or {}).get("tool_log", [])]


def test_explain_quotes_verified_citations_and_names_the_tier(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    with runner.Session(acme, settings, providers) as session:
        turn = session.ask("Why did HR-1 fail for Jane Smith?", now=NOW)
        assert turn.answer.intent == "explain"
        assert _tools(turn)[:2] == ["list_findings", "get_verdict"]
        assert "HR-1 is fail" in turn.answer.text and "tier 1" in turn.answer.text
        assert turn.pending == () and turn.answer.requested_actions == ()
        assert turn.trajectory == TRAJECTORY
        # a tier 2 verdict carries citations; the copilot quotes them and they verify
        gh4 = session.ask("Why did GH-4 pass for the platform repository?", now=NOW)
        assert gh4.answer.intent == "explain" and "tier 2" in gh4.answer.text
        assert gh4.answer.citations and all(c.verified for c in gh4.answer.citations)
        assert not any(e.startswith("citation_") for e in gh4.answer.guardrail_events)
        assert gh4.answer.turn == 2
        row = threads.get(acme, session.thread_id)
        assert row is not None and tuple(row.trajectory) == TRAJECTORY * 2
        assert session.thread_id.startswith("acme:copilot:")
        stored = schema.for_thread(acme, session.thread_id)
        assert [a.turn for a in stored] == [1, 2] and stored[0].latency_ms is not None


def test_status_reports_the_rollup_and_never_says_ready(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    with runner.Session(acme, settings, providers) as session:
        turn = session.ask("Are we SOC 2 ready?", now=NOW)
    assert turn.answer.intent == "status"
    assert "get_framework_rollup" in _tools(turn) and "list_findings" in _tools(turn)
    assert says_forbidden(turn.answer.text, ("ready", "compliant")) == []
    assert "Computed state" in turn.answer.text and "soc2" in turn.answer.text
    assert "Two requirements have a failing subject." in turn.answer.text
    assert {"forbidden_summary_word:ready", "forbidden_summary_word:compliant"} <= set(
        turn.answer.guardrail_events
    )


def test_missing_says_no_investigation_exists_and_offers_one(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    with runner.Session(acme, settings, providers) as session:
        turn = session.ask("What evidence is missing for GH-4?", now=NOW)  # never investigated
    assert turn.answer.intent == "missing"
    assert "get_investigation" in _tools(turn)
    assert "No investigation exists" in turn.answer.text and "open one" in turn.answer.text


def test_other_says_the_question_is_outside_the_data(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    with runner.Session(acme, settings, providers) as session:
        turn = session.ask("Who won the league last season?", now=NOW)
    assert turn.answer.intent == "other"
    assert turn.answer.text.startswith("I have no data for that question.")


def test_act_opens_nothing_until_confirmed_then_exactly_one_approval(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    open_before = {a.approval_id for a in approvals.list_items(acme, "remediation", True)}
    with runner.Session(acme, settings, providers) as session:
        turn = session.ask("Fix HR-1 for Jane Smith.", now=NOW)
        assert turn.answer.intent == "act"
        assert [p.action for p in turn.pending] == ["remove_repo_collaborator"]
        assert turn.pending[0].target == {"repo": "acme-example/platform", "login": "jsmith"}
        assert "Nothing has been changed" in turn.answer.text
        assert {a.approval_id for a in approvals.list_items(acme, "remediation", True)} == (
            open_before
        )
        assert "Would open 1 approval item" in runner.describe(turn)
        opened = session.confirm(turn, now=NOW)
        assert len(opened.approval_ids) == 1 and len(opened.investigation_ids) == 1
        item = approvals.get(acme, opened.approval_ids[0])
        assert item is not None and item.kind == "remediation" and item.is_open
        assert investigations.for_verdict(acme, turn.pending[0].verdict_id) is not None
        again = session.confirm(turn, now=NOW)  # idempotent: same proposal row, same item
        assert again.approval_ids == opened.approval_ids and again.investigation_ids == ()


def test_three_requested_actions_yield_two_and_a_note(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    with runner.Session(acme, settings, providers) as session:
        turn = session.ask("Fix everything for Jane Smith.", now=NOW)
    assert [a["action"] for a in turn.answer.requested_actions] == [
        "remove_repo_collaborator",
        "remove_org_member",
    ]
    assert "requested_actions_capped:1" in turn.answer.guardrail_events
    assert "were not included" in turn.answer.text
    assert len(turn.pending) == 2


def test_an_exhausted_daily_budget_answers_from_computed_state_without_a_model(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    later = NOW + timedelta(days=2)  # a day no other test spends in
    budget_gate.record_spend(acme, 100.0, 0, None, later)
    calls_before = len(providers.llm.calls)
    with runner.Session(acme, settings, providers) as session:
        turn = session.ask("Are we SOC 2 ready?", now=later)
        stored = schema.for_thread(acme, session.thread_id)
    assert len(providers.llm.calls) == calls_before  # no model call was made
    assert "budget_exhausted:daily" in turn.answer.guardrail_events
    assert turn.answer.intent == "status" and "Computed state" in turn.answer.text
    assert "daily model budget is exhausted" in turn.answer.text
    assert turn.pending == () and turn.answer.model is None and turn.trajectory == ()
    assert [a.turn for a in stored] == [1] and stored[0].guardrail_events == (
        "budget_exhausted:daily",
    )


def test_a_turn_records_its_spend_in_the_daily_ledger(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    before = budget_gate.daily_spend(acme, NOW)
    with runner.Session(acme, settings, providers) as session:
        session.ask("Why did GH-4 pass for the platform repository?", now=NOW)
    assert budget_gate.daily_spend(acme, NOW) > before
