"""S6 and S21 (spec.md Layer 13): the planted token in acme's file listing fails GH-5, reaches
no model unredacted (neither through the evidence fence nor through a tool result), and
appears in the step log and the tracing payload only in its redacted form."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ccp.agent import tier3 as _tier3  # noqa: F401 - registers tier 3
from ccp.agent.tools import ToolContext, bind_tools
from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.copilot import runner
from ccp.evaluators import runner as evaluations
from ccp.evaluators.tier2 import Providers
from ccp.evaluators.verdict import latest_per_subject
from ccp.llm.client import SystemBlock, ToolCall
from ccp.observability import steps
from ccp.observability.tracing import RecordingSink
from ccp.retrieval import index
from ccp.security.secrets_patterns import scan
from ccp.storage.scoped import ScopedDB
from tests.component.test_copilot_stub import Stub, make_providers
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
REDACTED = "[REDACTED:github_pat_classic]"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, Providers]]:
    with prepared_tenants(base_settings, "ccp_s6", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = make_providers()
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("GH-5",))
        evaluations.drain(acme, settings, now=NOW, providers=providers)
        state_writer.drain(acme)
        yield settings, acme, providers


def _planted(acme: ScopedDB) -> tuple[str, str]:
    row = acme.fetch_one(
        "SELECT chunk_id, evidence_entity_id FROM chunks WHERE text LIKE %s LIMIT 1", ("%ghp_%",)
    )
    assert row is not None, "the acme file listing fixture carries a planted ghp_ token"
    return str(row[0]), str(row[1])


def test_gh5_fails_on_the_planted_token(world) -> None:  # type: ignore[no-untyped-def]
    _, acme, _ = world
    verdicts = latest_per_subject(acme, "GH-5")
    assert verdicts and any(v.status.value == "fail" for v in verdicts)
    # the verdict names the pattern and a six-character preview, never the whole secret
    assert all(scan(v.reasoning) == [] for v in verdicts)


def test_tool_results_carry_only_the_redacted_form(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    chunk_id, evidence_id = _planted(acme)
    tools = bind_tools(
        ToolContext(acme, providers.embedder, providers.reranker, providers.retrieval),
        ("get_chunk", "get_evidence_chunks", "search_evidence"),
    )
    got = tools["get_chunk"].call({"chunk_id": chunk_id})
    assert not got["is_error"] and got["redactions"] >= 1
    assert REDACTED in got["result"]["text"] and "ghp_" not in got["result"]["text"]
    listing = tools["get_evidence_chunks"].call({"evidence_entity_id": evidence_id})
    assert listing["redactions"] >= 1
    assert all("ghp_" not in c["text"] for c in listing["result"]["chunks"])
    found = tools["search_evidence"].call(
        {"query": "deploy script token", "evidence_type": "repo_file_listing"}
    )
    assert all("ghp_" not in c["text"] for c in found["result"]["chunks"])


class _ListingStub(Stub):
    """The copilot stub whose gather step reads the whole planted file listing."""

    def __init__(self, evidence_id: str) -> None:
        super().__init__()
        answer = self.handler

        def chat(
            system: list[SystemBlock], messages: list[dict[str, Any]], tools: list[dict[str, Any]]
        ) -> tuple[str, list[ToolCall]]:
            if sum(1 for m in messages if m.get("role") == "assistant") == 0:
                return "", [
                    ToolCall("c-1", "get_evidence_chunks", {"evidence_entity_id": evidence_id})
                ]
            return "Gathered.", []

        self.chat_handler = chat
        self.handler = answer


def test_prompts_step_log_and_trace_payload_hold_only_the_redacted_form(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    _, evidence_id = _planted(acme)
    stub_providers = Providers(
        embedder=providers.embedder,
        reranker=providers.reranker,
        llm=_ListingStub(evidence_id),
        retrieval=providers.retrieval,
    )
    sink = RecordingSink()
    steps.set_sink(sink)
    started = datetime.now(UTC)
    try:
        with runner.Session(acme, settings, stub_providers) as session:
            turn = session.ask("What is in the platform file listing?", now=NOW)
    finally:
        steps.set_sink(None)
    calls = (turn.answer.request_json or {}).get("calls") or {}
    answer_user = str(calls["answer"]["request"]["user"])
    assert REDACTED in answer_user and "ghp_" not in answer_user  # the fenced evidence (S6)
    assert any(
        e.startswith("secret_redacted_in_tool_result:get_evidence_chunks:")
        for e in turn.answer.guardrail_events
    )
    assert "ghp_" not in turn.answer.text
    since = [
        line
        for line in steps.read_lines()
        if datetime.fromisoformat(str(line.get("started_at"))) >= started
    ]
    assert since and all(scan(json.dumps(line, default=str)) == [] for line in since)  # S21
    assert sink.spans and all(scan(json.dumps(s, default=str)) == [] for s in sink.spans)
