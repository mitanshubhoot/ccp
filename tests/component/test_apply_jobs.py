"""MT2 (A-110): approvals as data, with an idempotent apply job.

The defect: a reviewer's decision was recorded and then applied in the same process; when
the apply failed (a model outage, a crash), the decision sat recorded and unapplied until a
person noticed and ran `ccp approvals resume`. Now the decision and its apply job commit in
one transaction; the deciding process is the job's first worker, a failure leaves the job to
be retried with backoff, and a job whose decision already took effect does nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ccp.agent import decisions, threads, tier3
from ccp.approvals import actions, store
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators.context import load_context
from ccp.knowledge import graph as knowledge
from ccp.llm.client import StubLLMClient
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


def _providers() -> tier3.Providers:
    return tier3.Providers(
        embedder=HashingEmbeddingClient(),
        reranker=LexicalOverlapReranker(),
        llm=StubLLMClient(handler=stub_extract_agent, chat_handler=stub_chat_search_once),
        retrieval=RetrievalConfig(),
    )


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, tier3.Providers]]:
    with prepared_tenants(base_settings, "ccp_applies", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = _providers()
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        # no critical systems list: HR-4 stops at the gate for a person every time
        acme.execute("DELETE FROM chunks WHERE evidence_type = 'critical_systems_list'")
        quick = settings.model_copy(update={"ccp_retry_base_seconds": 0.0})
        yield quick, acme, providers


def _interrupt(world: tuple[Settings, ScopedDB, tier3.Providers]) -> tuple[str, store.Approval]:
    settings, acme, providers = world
    spec = knowledge.load().tests["HR-4"]
    verdict = tier3.evaluate(
        acme, settings, load_context(acme, settings, spec, NOW, NOW), spec, "tenant", providers
    )
    assert verdict.status is VerdictStatus.needs_review
    thread_id = str((verdict.request_json or {})["thread_id"])
    item = store.for_ref(acme, "tier3_interrupt", thread_id)
    assert item is not None and item.is_open
    return thread_id, item


def _jobs(acme: ScopedDB, item: store.Approval) -> list[tuple[str, int]]:
    rows = acme.fetch_all(
        "SELECT status, attempts FROM event_log WHERE topic = 'applies' AND partition_key = %s"
        ' ORDER BY "offset"',
        (f"acme:approval:{item.approval_id}",),
    )
    return [(str(s), int(a)) for s, a in rows]


def test_a_decision_commits_with_its_job_and_is_applied_once(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    thread_id, item = _interrupt(world)
    outcome = actions.approve(acme, settings, item.approval_id, "auditor", providers=providers)
    assert outcome.ok and "thread resumed" in outcome.detail
    assert _jobs(acme, item) == [("done", 1)]  # queued with the decision, applied at once
    row = threads.get(acme, thread_id)
    assert row is not None and row.status == "finished"
    stored = acme.fetch_one(
        "SELECT count(*) FROM verdicts WHERE verdict_id = %s", (row.verdict_id,)
    )
    assert stored == (1,)
    jobs = decisions.ApplyJobs(acme, settings, providers)
    assert jobs.run_once() == 0  # nothing left to apply
    with acme.transaction() as cur:  # the same job delivered twice
        decided = store.get(acme, item.approval_id)
        assert decided is not None
        decisions.queue_apply(cur, acme, decided)
    assert jobs.run_once() == 1 and jobs.already_applied == 1
    assert _jobs(acme, item) == [("done", 1), ("done", 1)]
    after = threads.get(acme, thread_id)
    assert after is not None and after.verdict_id == row.verdict_id  # nothing was applied again


def test_a_failed_apply_is_retried_by_its_job(
    world: tuple[Settings, ScopedDB, tier3.Providers], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, acme, providers = world
    thread_id, item = _interrupt(world)
    real_resume = tier3.resume
    calls: list[str] = []

    def outage_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("approval_id", ""))
        if len(calls) == 1:
            raise RuntimeError("model outage")
        return real_resume(*args, **kwargs)

    monkeypatch.setattr(tier3, "resume", outage_once)
    outcome = actions.approve(acme, settings, item.approval_id, "auditor", providers=providers)
    assert not outcome.ok and "resume failed" in outcome.detail and "applies" in outcome.detail
    assert _jobs(acme, item) == [("pending", 1)]  # kept for a retry, not dead, not lost
    assert item.approval_id in {i.approval_id for i in decisions.unapplied(acme)}
    jobs = decisions.ApplyJobs(acme, settings, providers)  # any worker running `applies`
    assert jobs.run_once() == 1
    assert jobs.outcomes[item.approval_id].verdict is not None
    assert _jobs(acme, item) == [("done", 2)]
    row = threads.get(acme, thread_id)
    assert row is not None and row.status == "finished"
    assert item.approval_id not in {i.approval_id for i in decisions.unapplied(acme)}
    assert calls == [item.approval_id, item.approval_id]
    assert evaluations.unannounced(acme) == 0
