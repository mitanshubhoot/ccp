"""The approval stage: append-only items with derived state, the four kinds, and approving a
tier 3 interrupt from a separate process through `ccp approvals approve`."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest

from ccp.agent import threads, tier3
from ccp.approvals import actions, store
from ccp.config import Settings
from ccp.evaluators.context import load_context
from ccp.evaluators.verdict import latest_per_subject
from ccp.knowledge import graph as knowledge
from ccp.llm.client import StubLLMClient
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus
from tests.support.pipeline import prepared_tenants
from tests.support.stub_agent import stub_chat_search_once, stub_extract_agent

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB]]]:
    with prepared_tenants(base_settings, "ccp_appr", ("acme",)) as (settings, dbs):
        yield settings, dbs


def test_items_are_append_only_and_state_is_the_latest_event(world) -> None:  # type: ignore[no-untyped-def]
    _, dbs = world
    acme = dbs["acme"]
    approval_id = store.open_item(acme, "policy_draft", "draft-1", {"path": "data/drafts/x.md"})
    assert (
        store.open_item(acme, "policy_draft", "draft-1", {"path": "other"}) == approval_id
    )  # still open
    item = store.get(acme, approval_id)
    assert (
        item is not None and item.state == "opened" and item.is_open and item.reviewer == "system"
    )
    with pytest.raises(ValueError):
        store.record_event(acme, approval_id, "approved", " ")
    changed = store.record_event(
        acme, approval_id, "changes_requested", "editor", "tighten section 2"
    )
    assert changed.state == "changes_requested" and changed.is_open
    approved = store.record_event(acme, approval_id, "approved", "cto", "fine now")
    assert approved.state == "approved" and not approved.is_open and approved.note == "fine now"
    with pytest.raises(ValueError):
        store.record_event(acme, approval_id, "rejected", "cto")
    assert [e["event"] for e in store.history(acme, approval_id)] == [
        "opened",
        "changes_requested",
        "approved",
    ]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        acme.execute("DELETE FROM approval_events")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        acme.execute("UPDATE approvals SET kind = 'remediation'")
    reopened = store.open_item(acme, "policy_draft", "draft-1", {"path": "v2"})
    assert reopened != approval_id
    assert [i.approval_id for i in store.list_items(acme, "policy_draft", open_only=True)] == [
        reopened
    ]


def test_payload_hash_is_canonical(world) -> None:  # type: ignore[no-untyped-def]
    assert store.payload_hash({"b": 1, "a": [1, 2]}) == store.payload_hash({"a": [1, 2], "b": 1})
    assert store.payload_hash({"a": 1}) != store.payload_hash({"a": 2})


def test_tier3_interrupt_becomes_an_approval_item_and_the_cli_resumes_it(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs = world
    acme = dbs["acme"]
    providers = tier3.Providers(
        embedder=HashingEmbeddingClient(),
        reranker=None,
        llm=StubLLMClient(handler=stub_extract_agent, chat_handler=stub_chat_search_once),
        retrieval=RetrievalConfig(),
    )
    index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
    acme.execute("DELETE FROM chunks WHERE evidence_type = 'critical_systems_list'")
    spec = knowledge.load().tests["HR-4"]
    ctx = load_context(acme, settings, spec, NOW, NOW)
    verdict = tier3.evaluate(acme, settings, ctx, spec, "tenant", providers)
    assert verdict.status is VerdictStatus.needs_review
    thread_id = (verdict.request_json or {})["thread_id"]
    item = store.for_ref(acme, "tier3_interrupt", thread_id)
    assert item is not None and item.is_open and item.payload["draft"]["status"] == "needs_review"
    env = {
        **os.environ,
        "DATABASE_URL": settings.database_url,
        "DATABASE_ADMIN_URL": settings.database_admin_url,
        "CCP_DATA_DIR": str(settings.ccp_data_dir),
        "ANTHROPIC_API_KEY": "",
    }
    listed = subprocess.run(
        [sys.executable, "-m", "ccp", "approvals", "list", "--tenant", "acme", "--open"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert listed.returncode == 0 and item.approval_id in listed.stdout
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "ccp",
            "approvals",
            "approve",
            item.approval_id,
            "--tenant",
            "acme",
            "--reviewer",
            "auditor",
            "--decision",
            "override",
            "--status",
            "partial",
            "--note",
            "inventory pending",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "approved by auditor" in done.stdout and "thread resumed: partial" in done.stdout
    decided = store.get(acme, item.approval_id)
    assert decided is not None and decided.state == "approved" and decided.reviewer == "auditor"
    row = threads.get(acme, thread_id)
    assert (
        row is not None and row.status == "finished" and row.trajectory[-2:] == ["gate", "finalize"]
    )
    final = {v.subject_key: v for v in latest_per_subject(acme, "HR-4")}["HR-4|tenant"]
    assert final.status is VerdictStatus.partial and final.trigger_reason == "human_decision"
    with pytest.raises(ValueError):
        actions.approve(acme, settings, item.approval_id, "again")


def test_rejecting_an_interrupt_resumes_as_needs_review(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs = world
    acme = dbs["acme"]
    providers = tier3.Providers(
        embedder=HashingEmbeddingClient(),
        reranker=None,
        llm=StubLLMClient(handler=stub_extract_agent, chat_handler=stub_chat_search_once),
        retrieval=RetrievalConfig(),
    )
    acme.execute("DELETE FROM chunks WHERE evidence_type = 'critical_systems_list'")
    spec = knowledge.load().tests["HR-4"]
    ctx = load_context(acme, settings, spec, NOW, NOW)
    verdict = tier3.evaluate(acme, settings, ctx, spec, "tenant", providers)
    thread_id = (verdict.request_json or {})["thread_id"]
    item = store.for_ref(acme, "tier3_interrupt", thread_id)
    assert item is not None
    outcome = actions.reject(acme, settings, item.approval_id, "auditor", "not enough evidence")
    assert outcome.approval.state == "rejected" and "needs_review" in outcome.detail
    row = threads.get(acme, thread_id)
    assert row is not None and row.status == "finished" and row.decision is not None
    assert row.decision["action"] == "override" and row.decision["status"] == "needs_review"
