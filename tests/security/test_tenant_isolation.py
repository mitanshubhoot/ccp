"""S1, S5, S15 (spec.md Layer 13): tenant scoping of every evidence and state query.

(a) an agent seeded to look for the other tenant's data sees only its own tenant's chunks,
    a real foreign chunk id is not found, and a tier 3 verdict cites only its tenant's chunks;
    tenant is never a tool argument (S5);
(b) row-level security holds on a raw connection with no WHERE clause, including a pgvector
    nearest-neighbour query, and an unscoped session raises rather than returning zero rows;
(c) a thread id names its tenant and a mismatch is refused before the saver is touched;
(S15) an empty tenant reads as no_evaluations_yet, not as an empty list.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
import typer

from ccp.agent import tier3
from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.cli import _guarded_tenant
from ccp.agent.tools import ToolContext, bind_tools
from ccp.compliance_state import reader
from ccp.config import Settings
from ccp.evaluators.context import load_context
from ccp.evaluators.tier2 import Providers
from ccp.knowledge import graph as knowledge
from ccp.llm.client import StubLLMClient
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient, vector_literal
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants
from tests.support.stub_agent import stub_chat_search_once, stub_extract_agent

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB], Providers]]:
    with prepared_tenants(base_settings, "ccp_s1") as (settings, dbs):
        providers = Providers(
            embedder=HashingEmbeddingClient(),
            reranker=None,
            llm=StubLLMClient(handler=stub_extract_agent, chat_handler=stub_chat_search_once),
            retrieval=RetrievalConfig(),
        )
        for db in dbs.values():
            index.refresh(db, settings, providers.embedder, contextual_enrichment=False)
        yield settings, dbs, providers


def _chunk_ids(db: ScopedDB) -> set[str]:
    return {str(r[0]) for r in db.fetch_all("SELECT chunk_id FROM chunks")}


@pytest.mark.db
def test_agent_cannot_reach_other_tenant(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, providers = world
    acme, globex = dbs["acme"], dbs["globex"]
    acme_ids, globex_ids = _chunk_ids(acme), _chunk_ids(globex)
    assert acme_ids and globex_ids and not (acme_ids & globex_ids)
    tools = bind_tools(
        ToolContext(acme, providers.embedder, providers.reranker, providers.retrieval),
        tuple(sorted(ALLOWLISTS["copilot"])),
    )
    # a search seeded for the other tenant's data returns only this tenant's chunks
    found = tools["search_evidence"].call(
        {"query": "Globex critical systems", "evidence_type": "critical_systems_list"}
    )
    assert not found["is_error"]
    returned = {str(c["chunk_id"]) for c in found["result"]["chunks"]}
    assert returned and returned <= acme_ids
    # a real globex chunk id does not exist from acme's closure
    assert tools["get_chunk"].call({"chunk_id": next(iter(globex_ids))})["is_error"]
    # S5: tenant is not an argument any tool accepts
    refused = tools["search_evidence"].call(
        {"query": "x", "evidence_type": "critical_systems_list", "tenant_id": "globex"}
    )
    assert refused["is_error"] and "invalid arguments" in refused["error"]
    # the tier 3 verdict for acme retrieved and cited acme chunks only
    spec = knowledge.load().tests["HR-4"]
    ctx = load_context(acme, settings, spec, NOW, NOW)
    verdict = tier3.evaluate(acme, settings, ctx, spec, "tenant", providers)
    assert verdict.retrieved_chunk_ids and set(verdict.retrieved_chunk_ids) <= acme_ids
    assert all(c.chunk_id in acme_ids for c in verdict.citations)


@pytest.mark.db
def test_rls_holds_without_scoped_db(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, providers = world
    acme_count = index.chunk_count(dbs["acme"])
    assert acme_count > 0
    vector = vector_literal(providers.embedder.embed(["critical systems"], "query").vectors[0])
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("SET app.tenant_id = 'acme'")
        row = conn.execute("SELECT count(*) FROM chunks").fetchone()
        assert row is not None and int(row[0]) == acme_count
        nearest = conn.execute(
            "SELECT tenant_id FROM chunks ORDER BY embedding <=> %s::vector LIMIT 5", (vector,)
        ).fetchall()
        assert nearest and {str(r[0]) for r in nearest} == {"acme"}
    with psycopg.connect(settings.database_url) as conn, pytest.raises(psycopg.Error):
        conn.execute("SELECT count(*) FROM chunks").fetchone()  # unscoped: raises, never 0


@pytest.mark.db
def test_thread_id_tenant_guard(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, _ = world
    foreign = "globex:HR-4:HR-4|tenant:01ARZ3NDEKTSV4RRFFQ69G5FAV"
    with pytest.raises(typer.BadParameter, match="belongs to tenant 'globex'"):
        _guarded_tenant(settings, foreign, "acme")
    with pytest.raises(tier3.ThreadTenantMismatch):
        tier3.resume(dbs["acme"], settings, foreign, {"action": "approve", "reviewer": "x"})


@pytest.mark.db
def test_an_empty_tenant_reads_as_no_evaluations_yet(world) -> None:  # type: ignore[no-untyped-def]
    _, dbs, _ = world
    rollup = reader.framework_rollup(dbs["globex"], "soc2")
    assert rollup.no_evaluations_yet is True
    assert (
        all(r.state == "pending" or r.state == "no_evaluations_yet" for r in rollup.requirements)
        or not rollup.requirements
    )


def test_no_tenant_predicate_in_sql_outside_scoped_py() -> None:
    """Hygiene, not the guarantee (RLS is): application SQL never filters by tenant itself."""
    pattern = re.compile(r"WHERE[^\n\"']*tenant_id\s*=", re.IGNORECASE)
    offenders = sorted(
        str(path.relative_to(REPO))
        for path in (REPO / "ccp").rglob("*.py")
        if path.name != "scoped.py" and pattern.search(path.read_text(encoding="utf-8"))
    )
    assert offenders == []
