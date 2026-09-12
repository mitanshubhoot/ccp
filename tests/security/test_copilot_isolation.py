"""S9: the Copilot cannot see or name another tenant. Acme's copilot is asked about Globex."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ccp.config import Settings
from ccp.copilot import runner
from ccp.evaluators.tier2 import Providers
from ccp.retrieval import index
from ccp.storage.scoped import ScopedDB
from tests.component.test_copilot_stub import make_providers
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
GLOBEX_ONLY = ("Payroll-GX", "janesmith-gx", "okta-gx", "Globex Security", "Globex Corporation")


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB], Providers]]:
    with prepared_tenants(base_settings, "ccp_s9") as (settings, dbs):
        providers = make_providers()
        for db in dbs.values():
            index.refresh(db, settings, providers.embedder, contextual_enrichment=False)
        yield settings, dbs, providers


def test_acme_copilot_has_no_data_about_globex(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, providers = world
    globex_chunks = {r[0] for r in dbs["globex"].fetch_all("SELECT chunk_id FROM chunks")}
    assert globex_chunks
    with runner.Session(dbs["acme"], settings, providers) as session:
        turn = session.ask("What did Globex's most recent access review cover?", now=NOW)
    request = turn.answer.request_json or {}
    searched = [t for t in request.get("tool_log", []) if t.get("tool") == "search_evidence"]
    assert searched and not any(t.get("is_error") for t in searched)
    assert not set(request.get("retrieved_chunk_ids", [])) & globex_chunks
    assert turn.answer.text.startswith("I have no data for that question.")
    for token in GLOBEX_ONLY:
        assert token.lower() not in turn.answer.text.lower(), token
    assert session.thread_id.startswith("acme:")
