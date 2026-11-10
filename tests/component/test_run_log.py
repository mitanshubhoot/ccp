"""MT2 (A-109): the agent run log in Postgres, resumes that never sample a model twice, and
replays with an empty request diff.

The defects: a tool loop kept only its first request and last response, and its memo of
turns already taken lived in the process, so a run resumed by a new process (a crash, a
retried job) sampled every turn of the interrupted node again; the fence nonce was drawn
afresh per graph build, so even an identical history made a different request; and nothing
could show that a recorded run reproduces.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ccp.agent import threads, tier3
from ccp.config import Settings
from ccp.evaluators.context import load_context
from ccp.investigation import runner as investigation
from ccp.knowledge import graph as knowledge
from ccp.llm.client import StubLLMClient, SystemBlock, ToolCall
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.retrieval.rerank import LexicalOverlapReranker
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus
from tests.support.pipeline import prepared_tenants
from tests.support.stub_agent import stub_chat_search_once, stub_extract_agent

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
TRIGGER = datetime(2026, 9, 2, 15, tzinfo=UTC)


class _Chat:
    """The search-once stub, counting turns by request; it fails the second turn once."""

    def __init__(self) -> None:
        self.turns: list[int] = []
        self.failed = False

    def __call__(
        self, system: list[SystemBlock], messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[str, list[ToolCall]]:
        self.turns.append(len(messages))
        if len(messages) > 1 and not self.failed:
            self.failed = True
            raise RuntimeError("the worker died mid-loop")
        return stub_chat_search_once(system, messages, tools)


def _providers(chat: Any) -> tier3.Providers:
    return tier3.Providers(
        embedder=HashingEmbeddingClient(),
        reranker=LexicalOverlapReranker(),
        llm=StubLLMClient(handler=stub_extract_agent, chat_handler=chat),
        retrieval=RetrievalConfig(),
    )


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_runlog", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        index.refresh(acme, settings, HashingEmbeddingClient(), contextual_enrichment=False)
        # no critical systems list: the gather fallback's tool loop runs (two model turns)
        acme.execute("DELETE FROM chunks WHERE evidence_type = 'critical_systems_list'")
        yield settings, acme


def _evaluate(settings: Settings, acme: ScopedDB, providers: tier3.Providers):  # type: ignore[no-untyped-def]
    spec = knowledge.load().tests["HR-4"]
    ctx = load_context(acme, settings, spec, TRIGGER, NOW, reason="full_run")
    return tier3.evaluate(acme, settings, ctx, spec, "tenant", providers, reason="full_run")


def _steps(acme: ScopedDB, thread_id: str) -> dict[str, int]:
    rows = acme.fetch_all(
        "SELECT kind, count(*) FROM agent_steps WHERE thread_id = %s GROUP BY kind", (thread_id,)
    )
    return {str(k): int(n) for k, n in rows}


def test_a_resumed_run_is_served_its_recorded_turns_and_samples_none_twice(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme = world
    chat = _Chat()
    providers = _providers(chat)
    with pytest.raises(RuntimeError, match="died mid-loop"):
        _evaluate(settings, acme, providers)
    assert chat.turns == [1, 3]  # the first turn, then the second, which failed
    failed = acme.fetch_one(
        "SELECT thread_id FROM agent_threads WHERE graph = 'tier3' AND status = 'failed'"
    )
    assert failed is not None
    thread_id = str(failed[0])
    assert _steps(acme, thread_id) == {"nonce": 1, "model": 1, "tool": 1}

    verdict = _evaluate(settings, acme, providers)  # a retry: the same key resumes the run
    assert verdict.status is VerdictStatus.needs_review
    # the first turn and its search came from the log; only the failed turn was sampled again
    assert chat.turns == [1, 3, 3]
    assert (verdict.request_json or {})["thread_id"] == thread_id
    recorded = _steps(acme, thread_id)
    # the two turns and the scope extraction; the systems extraction has no evidence to
    # read without the list, so it makes no call
    assert recorded["model"] == 3 and recorded["tool"] == 1 and recorded["nonce"] >= 1


def test_a_recorded_run_replays_from_its_log_with_no_request_diff(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme = world
    row = acme.fetch_one(
        "SELECT thread_id, trajectory FROM agent_threads WHERE graph = 'tier3'"
        " AND status = 'interrupted' ORDER BY created_at DESC LIMIT 1"
    )
    assert row is not None
    thread_id = str(row[0])
    providers = _providers(stub_chat_search_once)
    report = tier3.replay(acme, settings, thread_id, providers)
    assert report.diverged is None
    assert report.trajectory[-1] == "interrupt"
    assert report.served == sum(_steps(acme, thread_id).values())
    # another model makes other requests: the replay names the first one the log lacks
    other = settings.model_copy(update={"ccp_tier3_model": "claude-sonnet-5-5"})
    changed = tier3.replay(acme, other, thread_id, providers)
    assert changed.diverged is not None and "not in the recorded run" in changed.diverged


def test_a_run_records_the_state_it_started_from(world) -> None:  # type: ignore[no-untyped-def]
    _, acme = world
    row = acme.fetch_one(
        "SELECT thread_id FROM agent_threads WHERE graph = 'tier3' ORDER BY created_at LIMIT 1"
    )
    assert row is not None
    started = threads.input_of(acme, str(row[0]))
    assert started is not None and started["test_id"] == "HR-4"
    assert started["thread_id"] == str(row[0])


def test_an_investigation_has_one_run_per_verdict(world) -> None:  # type: ignore[no-untyped-def]
    _, acme = world
    first = investigation.thread_id_of(acme, "01VERDICT")
    assert (
        first == investigation.thread_id_of(acme, "01VERDICT") == "acme:investigate:01VERDICT:run"
    )
