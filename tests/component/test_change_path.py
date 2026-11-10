"""NT6 (A-101) against a scratch database: the change path prepares like the full run, the
impact step stays inside its declared reach, and every new verdict names its rule."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ccp.compliance_state import run as pipeline
from ccp.config import Settings
from ccp.connectors.base import SourceEvent
from ccp.evaluators import runner as evaluations
from ccp.evaluators.impact import IMPACT_REACH, requests_for_entity
from ccp.evaluators.rule_versions import rule_stamp
from ccp.evaluators.tier2 import Providers
from ccp.evaluators.verdict import latest_per_subject
from ccp.knowledge import graph as knowledge
from ccp.remediation import reevaluate
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
KEYLESS = Providers(
    embedder=HashingEmbeddingClient(), reranker=None, llm=None, retrieval=RetrievalConfig()
)


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB]]]:
    with prepared_tenants(base_settings, "ccp_change") as (settings, dbs):
        yield settings, dbs


class _Nothing:
    name = "manual"
    source = "fixture"
    scopes: tuple[str, ...] = ()  # sees nothing, so its sync run is complete (A-118)

    def pull(
        self, tenant: TenantContext, since: datetime | None = None, report: object = None
    ) -> Iterator[SourceEvent]:
        return iter(())


def test_the_change_path_prepares_like_the_full_run(
    world: tuple[Settings, dict[str, ScopedDB]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, dbs = world
    calls: list[str] = []
    real = pipeline.prepare_changes

    def spy(db: ScopedDB, s: Settings, providers: Any) -> Any:
        calls.append(db.tenant.tenant_id)
        return real(db, s, providers)

    monkeypatch.setattr(reevaluate, "prepare_changes", spy)
    reevaluate.reevaluate(dbs["acme"], settings, _Nothing(), KEYLESS)  # type: ignore[arg-type]
    assert calls == ["acme"]  # identities resolved and the index refreshed before impact


@pytest.mark.parametrize("tenant", ["acme", "globex"])
def test_the_impact_step_stays_inside_its_declared_reach(
    world: tuple[Settings, dict[str, ScopedDB]], tenant: str
) -> None:
    _, dbs = world
    db = dbs[tenant]
    emitted: dict[str, set[str]] = {}
    for table in IMPACT_REACH:
        view = {"person_links": "current_person_links"}.get(table, f"current_{table}")
        column = "person_entity_id" if table == "person_links" else "entity_id"
        for (entity_id,) in db.fetch_all(f"SELECT {column} FROM {view}"):
            for request in requests_for_entity(db, table, str(entity_id), NOW):
                emitted.setdefault(table, set()).add(request.test_id)
    for table, tests in emitted.items():
        assert tests <= IMPACT_REACH[table], (table, tests - IMPACT_REACH[table])
    assert set().union(*emitted.values())  # the fixtures exercise the table, not just its shape


def test_every_new_verdict_names_its_rule(world: tuple[Settings, dict[str, ScopedDB]]) -> None:
    settings, dbs = world
    globex = dbs["globex"]
    evaluations.enqueue_all(globex, settings, now=NOW, tiers=(1,))
    evaluations.drain(globex, settings, now=NOW)
    verdicts = latest_per_subject(globex)
    assert verdicts
    tests = knowledge.load().tests
    for verdict in verdicts:
        expected = rule_stamp(tests[verdict.test_id], globex.tenant.settings)
        assert verdict.rule_version == expected, verdict.test_id
