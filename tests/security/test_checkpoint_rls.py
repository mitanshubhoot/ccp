"""S14: the checkpoint tables are under row-level security on thread_id, and every thread id
this code mints starts with its tenant."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest

from ccp.agent import tier3
from ccp.config import Settings
from ccp.evaluators.context import load_context
from ccp.knowledge import graph as knowledge
from ccp.llm.client import StubLLMClient
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage import checkpoints
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from tests.support.pipeline import prepared_tenants
from tests.support.stub_agent import stub_chat_search_once, stub_extract_agent

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB]]]:
    with prepared_tenants(base_settings, "ccp_ckpt") as (settings, dbs):
        providers = tier3.Providers(
            embedder=HashingEmbeddingClient(),
            reranker=None,
            llm=StubLLMClient(handler=stub_extract_agent, chat_handler=stub_chat_search_once),
            retrieval=RetrievalConfig(),
        )
        spec = knowledge.load().tests["HR-4"]
        for db in dbs.values():
            index.refresh(db, settings, providers.embedder, contextual_enrichment=False)
            ctx = load_context(db, settings, spec, NOW, NOW)
            tier3.evaluate(db, settings, ctx, spec, "tenant", providers)
        yield settings, dbs


def test_every_minted_thread_id_starts_with_the_tenant() -> None:
    settings = Settings()
    acme = TenantContext.for_id(settings, "acme")
    assert checkpoints.thread_id_for(acme, "HR-4", "HR-4|tenant", "01RUN").startswith("acme:")
    assert checkpoints.tenant_of_thread("globex:HR-4|tenant:01RUN") == "globex"


def test_raw_read_of_another_tenants_thread_is_empty_and_unscoped_raises(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    settings, dbs = world
    for tenant in ("acme", "globex"):
        rows = dbs[tenant].fetch_all("SELECT thread_id FROM agent_threads")
        assert rows and all(str(r[0]).startswith(f"{tenant}:") for r in rows)
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("SELECT set_config('search_path', 'checkpoints,public', false)")
        conn.execute("SELECT set_config('app.tenant_id', 'acme', false)")
        for table in checkpoints.THREAD_TABLES:
            ids = [str(r[0]) for r in conn.execute(f"SELECT thread_id FROM {table}").fetchall()]
            assert ids, table
            assert all(i.startswith("acme:") for i in ids), table
        conn.execute("SELECT set_config('app.tenant_id', 'globex', false)")
        ids = [str(r[0]) for r in conn.execute("SELECT thread_id FROM checkpoints").fetchall()]
        assert ids and not any(i.startswith("acme:") for i in ids)
        conn.execute("SELECT set_config('app.tenant_id', '', false)")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("SELECT count(*) FROM checkpoints").fetchone()


def test_saver_sessions_carry_the_schema_and_each_transaction_the_tenant(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    """A-99: the session carries no tenant; the saver sets it per transaction."""
    settings, dbs = world
    pool = checkpoints.saver_pool(settings, dbs["globex"].tenant, max_size=1)
    try:
        with pool.connection() as conn:
            unset = conn.execute(
                "SELECT current_setting('app.tenant_id', true) AS tenant"
            ).fetchone()
            assert unset is not None and not unset["tenant"]
            path = conn.execute("SHOW search_path").fetchone()
            assert path is not None and "checkpoints" in str(path["search_path"])
            with pytest.raises(psycopg.Error):  # no tenant: the policy refuses to answer
                conn.execute("SELECT thread_id FROM checkpoints").fetchall()
            with conn.transaction():
                conn.execute("SELECT set_config('app.tenant_id', 'globex', true)")
                ids = [
                    r["thread_id"]
                    for r in conn.execute("SELECT thread_id FROM checkpoints").fetchall()
                ]
            assert ids and all(i.startswith("globex:") for i in ids)
    finally:
        pool.close()
