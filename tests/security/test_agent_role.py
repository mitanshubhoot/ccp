"""MT3 (A-115): agent tools read through a read-only database role.

The defect: tools ran on the application role, which may insert into most tables, so a tool
bug, or a tool a model could steer, could write. Now every tool reads through its own role:
SELECT on what tools read, under row-level security, with a statement time limit, and no
INSERT, UPDATE or DELETE anywhere, which the catalogue lint checks.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from ccp.agent.authz import Gate
from ccp.agent.tools import TOOL_SPECS, ToolContext, bind_tools
from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.db import agent_pool, agent_role_and_password, lint_catalogue
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_agentrole", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        index.refresh(acme, settings, HashingEmbeddingClient(), contextual_enrichment=False)
        evaluations.enqueue_all(acme, settings, now=NOW, tiers=(1,))
        evaluations.drain(acme, settings, now=NOW)
        state_writer.drain(acme)
        pool = agent_pool(settings)
        try:
            yield settings, acme, ScopedDB(pool, TenantContext.for_id(settings, "acme"))
        finally:
            pool.close()


def test_every_tool_works_through_the_read_only_role(world) -> None:  # type: ignore[no-untyped-def]
    _, acme, readonly = world
    tools = bind_tools(
        ToolContext(readonly, HashingEmbeddingClient(), None, RetrievalConfig()),
        tuple(sorted(TOOL_SPECS)),
        gate=Gate("graph:copilot"),
    )
    chunk = acme.fetch_one("SELECT chunk_id, evidence_entity_id FROM chunks LIMIT 1")
    verdict = acme.fetch_one("SELECT verdict_id, test_id, subject_key FROM verdicts LIMIT 1")
    person = acme.fetch_one("SELECT entity_id FROM persons LIMIT 1")
    assert chunk and verdict and person
    calls: dict[str, dict[str, Any]] = {
        "search_evidence": {"query": "access review", "evidence_type": "access_review_report"},
        "get_chunk": {"chunk_id": chunk[0]},
        "list_evidence": {"evidence_type": "access_review_report"},
        "get_evidence_chunks": {"evidence_entity_id": chunk[1]},
        "list_assets": {},
        "get_person": {"canonical_person_id": person[0]},
        "get_current_state": {"test_id": verdict[1], "subject_key": verdict[2]},
        "get_verdict": {"verdict_id": verdict[0]},
        "list_findings": {},
        "get_access_grants": {"canonical_person_id": person[0]},
        "get_control": {"control_id": "CTL-ACCESS-REVOKE"},
        "get_framework_rollup": {"framework_id": "soc2"},
        "get_investigation": {"verdict_id": verdict[0]},
    }
    for name, args in calls.items():
        result = tools[name].call(args)
        if result["is_error"]:  # only a lookup that finds nothing; never a permission error
            assert name == "get_investigation" and result["error"] == "not found", (name, result)


def test_the_read_only_role_cannot_write_and_sees_one_tenant(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, readonly = world
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        readonly.execute("INSERT INTO verdicts (verdict_id) VALUES ('x')")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        readonly.execute("UPDATE verdicts SET reasoning = 'changed'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        readonly.execute("SELECT 1 FROM event_log LIMIT 1")  # not a table any tool reads
    own = readonly.fetch_one("SELECT count(*) FROM verdicts")
    assert own == acme.fetch_one("SELECT count(*) FROM verdicts")
    globex = ScopedDB(readonly._pool, TenantContext.for_id(settings, "globex"))
    assert globex.fetch_one("SELECT count(*) FROM verdicts") == (0,)


def test_the_lint_fails_when_the_agent_role_is_given_a_write(world) -> None:  # type: ignore[no-untyped-def]
    settings, _, _ = world
    assert [str(f) for f in lint_catalogue(settings)] == []
    role, _ = agent_role_and_password(settings)
    with psycopg.connect(settings.database_admin_url, autocommit=True) as conn:
        conn.execute(f'GRANT INSERT ON verdicts TO "{role}"')
        try:
            found = [str(f) for f in lint_catalogue(settings)]
            assert any("verdicts" in f and "INSERT" in f for f in found), found
        finally:
            conn.execute(f'REVOKE INSERT ON verdicts FROM "{role}"')
