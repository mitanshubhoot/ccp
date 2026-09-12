"""Tier 3 as a fixed workflow, run against a scratch database with stub model calls:
trajectory exactness, the fallback firing only on a miss, the interrupt, the resume from a
separate process through the Postgres checkpoint, and the HR-4 tabled results."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ccp.agent import threads, tier3
from ccp.agent.graphs.tier3 import TRAJECTORY_HAPPY
from ccp.config import Settings
from ccp.evaluators import registry, runner
from ccp.evaluators.context import load_context
from ccp.evaluators.verdict import latest_per_subject
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
TRIGGER = datetime(2026, 9, 2, 15, tzinfo=UTC)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _providers() -> tier3.Providers:
    return tier3.Providers(
        embedder=HashingEmbeddingClient(),
        reranker=LexicalOverlapReranker(),
        llm=StubLLMClient(handler=stub_extract_agent, chat_handler=stub_chat_search_once),
        retrieval=RetrievalConfig(),
    )


@pytest.fixture(scope="module")
def world(
    base_settings: Settings,
) -> Iterator[tuple[Settings, dict[str, ScopedDB], tier3.Providers]]:
    with prepared_tenants(base_settings, "ccp_tier3") as (settings, dbs):
        providers = _providers()
        for db in dbs.values():
            index.refresh(db, settings, providers.embedder, contextual_enrichment=False)
        yield settings, dbs, providers


def _evaluate(world, tenant: str, providers: tier3.Providers | None = None):  # type: ignore[no-untyped-def]
    settings, dbs, default = world
    spec = knowledge.load().tests["HR-4"]
    ctx = load_context(dbs[tenant], settings, spec, TRIGGER, NOW, reason="full_run")
    return tier3.evaluate(
        dbs[tenant], settings, ctx, spec, "tenant", providers or default, reason="full_run"
    )


def _thread_of(db: ScopedDB, verdict) -> threads.ThreadRow:  # type: ignore[no-untyped-def]
    thread_id = (verdict.request_json or {})["thread_id"]
    row = threads.get(db, thread_id)
    assert row is not None
    return row


def test_tier3_is_registered_by_import() -> None:
    assert registry.evaluator_for(3) is tier3.evaluate and registry.evaluator_for(2) is not None


def test_acme_hr4_partial_names_the_missing_system_with_the_exact_trajectory(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, _ = world
    v = _evaluate(world, "acme")
    assert v.status is VerdictStatus.partial, v.reasoning
    assert "Payroll-SaaS" in v.reasoning and v.tier == 3
    assert v.extracted_fields and v.extracted_fields["critical_systems"] == [
        "GitHub",
        "Okta",
        "AWS-Prod",
        "Payroll-SaaS",
        "Datadog",
    ]
    assert v.extracted_fields["systems_in_scope"] == ["GitHub", "Okta", "AWS-Prod", "Datadog"]
    assert v.extracted_fields["review_date"] == "2026-07-15"
    assert v.confidence >= 0.7 and v.confidence_cap == 1.0
    assert all(c.verified for c in v.citations) and len(v.citations) >= 9
    assert not any(e.startswith("gather_fallback_used") for e in v.guardrail_events)
    row = _thread_of(dbs["acme"], v)
    assert row.status == "finished" and tuple(row.trajectory) == TRAJECTORY_HAPPY
    assert row.thread_id.startswith("acme:HR-4:HR-4|tenant:") and row.verdict_id == v.verdict_id
    assert "hris:evidence:doc:access-review-2026-q2.md" in v.evidence_refs  # the newest review
    assert "hris:evidence:doc:access-review-2026-q1.md" not in v.evidence_refs


def test_globex_hr4_passes_on_the_page_split_pdf_review(world) -> None:  # type: ignore[no-untyped-def]
    _, dbs, _ = world
    v = _evaluate(world, "globex")
    assert v.status is VerdictStatus.passed, v.reasoning
    assert v.extracted_fields and v.extracted_fields["critical_systems"] == [
        "GitHub",
        "Okta",
        "Payroll-GX",
    ]
    assert set(v.extracted_fields["systems_in_scope"]) == {"GitHub", "Okta", "Payroll-GX"}
    assert any(
        ref.startswith("manual:evidence:access-review-2026-h1.pdf") for ref in v.evidence_refs
    )
    pages = dbs["globex"].fetch_all(
        "SELECT DISTINCT page FROM chunks WHERE chunk_id = ANY(%s)", (list(v.retrieved_chunk_ids),)
    )
    assert {int(p[0]) for p in pages if p[0] is not None} >= {1, 2}
    assert tuple(_thread_of(dbs["globex"], v).trajectory) == TRAJECTORY_HAPPY


def test_missing_critical_list_fires_the_fallback_then_the_interrupt(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, providers = world
    acme = dbs["acme"]
    acme.execute("DELETE FROM chunks WHERE evidence_type = 'critical_systems_list'")
    try:
        v = _evaluate(world, "acme")
    finally:
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
    assert v.status is VerdictStatus.needs_review
    assert v.confidence <= 0.40 and "unresolved_evidence_cap" in v.guardrail_events
    assert "gather_fallback_used:critical_systems_list" in v.guardrail_events
    assert any(e.startswith("interrupted_for_review:") for e in v.guardrail_events)
    row = _thread_of(acme, v)
    assert row.status == "interrupted" and row.interrupt is not None
    assert row.interrupt["draft"]["status"] == "needs_review"
    assert tuple(row.trajectory) == (
        "fetch_critical_list",
        "gather_fallback",
        "extract_systems",
        "fetch_latest_review",
        "extract_scope",
        "verify_citations",
        "compare",
        "interrupt",
    )
    assert any(
        t["tool"] == "search_evidence"
        for t in (v.response_json or {}).get("interrupt", {}).get("tool_log", []) or []
    )


def test_resume_from_a_separate_process_completes_the_thread(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, providers = world
    acme = dbs["acme"]
    acme.execute("DELETE FROM chunks WHERE evidence_type = 'critical_systems_list'")
    try:
        v = _evaluate(world, "acme")
    finally:
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
    thread_id = (v.request_json or {})["thread_id"]
    env = {
        **os.environ,
        "DATABASE_URL": settings.database_url,
        "DATABASE_ADMIN_URL": settings.database_admin_url,
        "CCP_DATA_DIR": str(settings.ccp_data_dir),
        "ANTHROPIC_API_KEY": "",
    }
    # a tenant mismatch is refused before the saver is touched
    wrong = subprocess.run(
        [
            sys.executable,
            "-m",
            "ccp",
            "agent",
            "inspect",
            "--thread",
            thread_id,
            "--tenant",
            "globex",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert wrong.returncode != 0 and "belongs to tenant 'acme'" in (wrong.stdout + wrong.stderr)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "ccp",
            "agent",
            "resume",
            "--thread",
            thread_id,
            "--decision",
            "override",
            "--status",
            "fail",
            "--reviewer",
            "auditor",
            "--note",
            "no inventory on file",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "fail" in done.stdout
    row = threads.get(acme, thread_id)
    assert row is not None and row.status == "finished" and row.decision is not None
    assert row.decision["action"] == "override" and row.trajectory[-2:] == ["gate", "finalize"]
    heads = {x.subject: x for x in latest_per_subject(acme, "HR-4")}
    final = heads["tenant"]
    assert final.status is VerdictStatus.failed and final.trigger_reason == "human_decision"
    assert (
        "human override by auditor" in final.reasoning
        and "human_override:fail" in final.guardrail_events
    )
    assert final.verdict_id == row.verdict_id and final.evaluation_key != v.evaluation_key


def test_resume_approve_keeps_the_draft_status(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, providers = world
    acme = dbs["acme"]
    acme.execute("DELETE FROM chunks WHERE evidence_type = 'critical_systems_list'")
    try:
        v = _evaluate(world, "acme")
    finally:
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
    thread_id = (v.request_json or {})["thread_id"]
    final = tier3.resume(
        acme, settings, thread_id, {"action": "approve", "reviewer": "auditor"}, providers, now=NOW
    )
    assert final.status is VerdictStatus.needs_review and "human_approved" in final.guardrail_events
    assert "approved as needs_review by auditor" in final.reasoning
    with pytest.raises(LookupError):
        tier3.resume(
            acme, settings, thread_id, {"action": "approve", "reviewer": "again"}, providers
        )
    with pytest.raises(tier3.ThreadTenantMismatch):
        tier3.resume(
            dbs["globex"], settings, thread_id, {"action": "approve", "reviewer": "x"}, providers
        )


def test_full_run_routes_hr4_through_the_registry_and_is_idempotent(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, providers = world
    globex = dbs["globex"]
    assert runner.enqueue_all(globex, settings, now=NOW, tests=("HR-4",)) == 1
    summary = runner.drain(globex, settings, now=NOW, providers=providers)
    assert summary.evaluated == 1 and summary.by_status == {"pass": 1}
    runner.enqueue_all(globex, settings, now=NOW, tests=("HR-4",))
    again = runner.drain(globex, settings, now=NOW, providers=providers)
    assert again.duplicates == 1  # same trigger, same key: the second run writes nothing


def test_keyless_tier3_parks(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, _ = world
    acme = dbs["acme"]
    keyless = tier3.Providers(
        embedder=HashingEmbeddingClient(), reranker=None, llm=None, retrieval=RetrievalConfig()
    )
    runner.enqueue_all(acme, settings, now=NOW, tests=("HR-4",))
    summary = runner.drain(acme, settings, now=NOW, providers=keyless)
    assert summary.evaluated == 0 and runner.parked_count(acme) >= 1
    assert json.dumps(runner.parked_count(acme))  # a number, not an exception
