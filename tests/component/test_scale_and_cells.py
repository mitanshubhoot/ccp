"""LT9 (A-172): growth without a redesign, and residency. Connections work behind a
transaction-mode pooler, reports read a replica only while it keeps up, a large tenant
gets a partition of its own under the same row-level security, a tenant is served only by
its own cell, and an EU cell reaches models and embeddings in the EU.

The defects: one database in one region, with nothing in the code that would survive a
pooler, a replica, a partition or a second cell.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from ccp import cells, tenancy
from ccp.config import Settings
from ccp.retrieval.embeddings import VoyageEmbeddingClient, build_embedding_client
from ccp.storage import db, partitions
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext


def test_cells_map_models_to_their_region_and_refuse_the_rest() -> None:
    found = cells.load()
    us, eu = found["us-1"], found["eu-1"]
    assert us.provider == "anthropic" and us.model_id("claude-haiku-5-5") == "claude-haiku-5-5"
    assert eu.residency == "eu" and eu.provider == "bedrock" and eu.model_region == "eu-central-1"
    assert eu.model_id("claude-sonnet-5-5").startswith("eu.")
    with pytest.raises(ValueError, match="no bedrock profile"):
        eu.model_id("claude-fable-5-2")
    seen: list[dict[str, Any]] = []

    class _Messages:
        batches = None

        def create(self, **kw: Any) -> str:
            seen.append(kw)
            return "ok"

        parse = create

    routed = cells.ModelRouted(SimpleNamespace(messages=_Messages()), eu)
    routed.messages.create(model="claude-haiku-5-5", max_tokens=1)
    routed.messages.parse(model="claude-opus-5-5", max_tokens=1)
    assert [kw["model"] for kw in seen] == [
        eu.profiles["claude-haiku-5-5"],
        eu.profiles["claude-opus-5-5"],
    ]
    with pytest.raises(ValueError, match="CCP_CELL"):
        cells.current(Settings(ccp_cell="mars-1"))


def test_an_eu_cell_embeds_through_the_eu_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(ccp_cell="eu-1", ccp_embed_provider="voyage", voyage_api_key="k")  # type: ignore[arg-type]
    monkeypatch.setattr("ccp.retrieval.embeddings.require_voyage_terms", lambda s: None)
    client = build_embedding_client(settings)
    assert (
        isinstance(client, VoyageEmbeddingClient) and client.url == cells.load()["eu-1"].voyage_url
    )
    us = build_embedding_client(settings.model_copy(update={"ccp_cell": "us-1"}))
    assert isinstance(us, VoyageEmbeddingClient) and us.url == VoyageEmbeddingClient.url


def test_pooler_mode_turns_off_prepared_statements(base_settings: Settings) -> None:
    pooled = base_settings.model_copy(update={"database_pooler": True})
    assert db._session_kwargs(pooled) == {"prepare_threshold": None}
    assert db._session_kwargs(base_settings) == {}
    assert (
        db.listen_url(pooled.model_copy(update={"database_direct_url": "postgresql://direct"}))
        == "postgresql://direct"
    )
    assert db.listen_url(base_settings) == base_settings.database_url


@pytest.fixture
def scratch(base_settings: Settings) -> Iterator[Settings]:
    try:
        with db.scratch_database(base_settings, prefix="ccp_scale") as settings:
            tenancy.forget()
            yield settings
            tenancy.forget()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


@pytest.mark.db
def test_reports_read_the_replica_only_while_it_keeps_up(
    scratch: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = scratch.model_copy(
        update={"database_replica_url": scratch.database_url, "database_pooler": True}
    )
    primary = db.app_pool(settings, max_size=2)
    replica = db.replica_pool(settings)
    assert replica is not None and db.replica_pool(scratch) is None
    try:
        assert db.replica_lag(replica) == 0.0  # not replaying: as fresh as the primary
        assert db.read_pool(settings, primary, replica) is replica
        monkeypatch.setattr(db, "replica_lag", lambda pool: 120.0)
        assert db.read_pool(settings, primary, replica) is primary  # too far behind
        monkeypatch.setattr(db, "replica_lag", lambda pool: None)
        assert db.read_pool(settings, primary, replica) is primary  # cannot tell: the primary
        with replica.connection() as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TABLE nope (x int)")
        with primary.connection() as conn:  # behind a pooler: no server-side prepares
            for _ in range(8):
                conn.execute("SELECT 1").fetchone()
            assert conn.prepare_threshold is None
    finally:
        primary.close()
        replica.close()


@pytest.mark.db
def test_a_large_tenant_gets_its_own_partition_under_the_same_security(scratch: Settings) -> None:
    pool = db.app_pool(scratch, max_size=2)
    try:
        for tenant, n in (("acme", 3), ("globex", 2)):
            scoped = ScopedDB(pool, TenantContext.for_id(scratch, tenant))
            for i in range(n):
                scoped.execute(
                    "INSERT INTO tool_results (tenant_id, handle, thread_id, tool, content, chars,"
                    " tokens) VALUES (%s, %s, %s, 'search_evidence', 'x', 1, 1)",
                    (tenant, f"{tenant}:h{i}", f"{tenant}:run"),
                )
        app_role, _ = db.app_role_and_password(scratch)
        agent_role, _ = db.agent_role_and_password(scratch)
        with db.admin_connection(scratch.database_admin_url) as admin:
            with pytest.raises(ValueError, match="may not be partitioned"):
                partitions.partition_table(admin, "verdicts", app_role)
            assert partitions.partition_table(admin, "tool_results", app_role, agent_role)
            assert not partitions.partition_table(admin, "tool_results", app_role, agent_role)
            name = partitions.own_partition(admin, "tool_results", "acme")
            assert partitions.layout(admin, "tool_results") == {name: 3, "tool_results_default": 2}
            indexes = admin.execute(
                "SELECT count(*) FROM pg_indexes WHERE tablename = %s", (name,)
            ).fetchone()
            assert indexes is not None and indexes[0] >= 2  # its own copy of every index
        acme = ScopedDB(pool, TenantContext.for_id(scratch, "acme"))
        globex = ScopedDB(pool, TenantContext.for_id(scratch, "globex"))
        assert len(acme.fetch_all("SELECT handle FROM tool_results")) == 3  # through the parent
        assert len(globex.fetch_all("SELECT handle FROM tool_results")) == 2  # RLS still holds
        acme.execute(
            "INSERT INTO tool_results (tenant_id, handle, thread_id, tool, content, chars, tokens)"
            " VALUES ('acme', 'acme:h9', 'acme:run', 'search_evidence', 'x', 1, 1)"
        )
        with db.admin_connection(scratch.database_admin_url) as admin:
            assert partitions.layout(admin, "tool_results")[name] == 4  # new rows land in it
        assert db.lint_catalogue(scratch) == []  # a partition is its parent's
    finally:
        pool.close()


@pytest.mark.db
def test_a_tenant_is_served_only_by_its_own_cell(scratch: Settings) -> None:
    assert TenantContext.for_id(scratch, "acme").settings.cell == "us-1"
    tenancy.forget()
    with pytest.raises(tenancy.TenantUnavailable, match="lives in cell us-1"):
        TenantContext.for_id(scratch.model_copy(update={"ccp_cell": "eu-1"}), "acme")
