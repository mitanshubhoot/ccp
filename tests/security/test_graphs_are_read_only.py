"""S10: no graph has a write path, and drafts never become evidence on their own."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.tools import TOOL_FNS, TOOL_SPECS
from ccp.approvals import actions as approval_actions
from ccp.config import Settings
from ccp.evaluators.tier2 import Providers
from ccp.llm.client import StubLLMClient
from ccp.policy import runner
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from tests.support.members import member
from tests.support.pipeline import prepared_tenants
from tests.support.stub_policy import analyze, compose, gather

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, Providers]]:
    with prepared_tenants(base_settings, "ccp_s10", ("acme",)) as (settings, dbs):
        providers = Providers(
            embedder=HashingEmbeddingClient(),
            reranker=None,
            llm=StubLLMClient(handler=analyze, chat_handler=gather, complete_handler=compose),
            retrieval=RetrievalConfig(),
        )
        index.refresh(dbs["acme"], settings, providers.embedder, contextual_enrichment=False)
        yield settings, dbs["acme"], providers


def test_every_allowlisted_tool_is_in_the_read_only_registry() -> None:  # S10 (a)
    for graph, tools in ALLOWLISTS.items():
        assert tools <= set(TOOL_SPECS), graph
        assert tools <= set(TOOL_FNS), graph


def _counts(db: ScopedDB) -> tuple[int, int]:
    evidence = db.fetch_one("SELECT count(*) FROM evidence")
    chunks = db.fetch_one("SELECT count(*) FROM chunks")
    return int(evidence[0]), int(chunks[0])  # type: ignore[index]


def test_a_draft_changes_no_evidence_and_neither_does_approving_it(world) -> None:  # S10 (b), (c)
    settings, acme, providers = world
    before = _counts(acme)
    _, draft = runner.draft(acme, settings, "CTL-VULN-DISCLOSURE", providers, now=NOW)
    assert draft.path and _counts(acme) == before
    outcome = approval_actions.approve(
        acme, settings, draft.approval_id, member(acme, "cto"), "looks right"
    )
    assert outcome.approval.state == "approved"
    assert _counts(acme) == before  # only the manual upload path can change them
    assert acme.fetch_one("SELECT count(*) FROM evidence WHERE raw_ref LIKE %s", ("%drafts%",)) == (
        0,
    )
