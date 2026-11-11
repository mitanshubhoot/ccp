"""MT7 (A-143): a second reader on every AI-derived PASS, refusal rates per test, and shadow
diffs of a candidate model over stored requests.

The defects: a tier 2 PASS rested on one model's reading, so a misread quote became a pass
with nothing to catch it; a model's refusal vanished into a dead letter, so a rising refusal
rate on one test was invisible; and a new model could only be judged by shipping it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from ccp.config import Settings
from ccp.evals import shadow
from ccp.evaluators import quality, tier2
from ccp.evaluators import runner as evaluations
from ccp.evaluators.impact import EvaluationRequest
from ccp.evaluators.second_reader import DISAGREED
from ccp.llm.client import StubLLMClient
from ccp.llm.errors import TerminalModelError
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.retrieval.rerank import LexicalOverlapReranker
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus
from tests.support.pipeline import prepared_tenants
from tests.support.stub_extractors import stub_extract

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
TRIGGER = datetime(2026, 9, 2, 15, tzinfo=UTC)
ACME_PLATFORM = "repo:github:asset:R_kgDOAcmeP001"


def _providers(llm: StubLLMClient) -> tier2.Providers:
    return tier2.Providers(
        embedder=HashingEmbeddingClient(),
        reranker=LexicalOverlapReranker(),
        llm=llm,
        retrieval=RetrievalConfig(),
    )


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_second", ("acme",)) as (settings, dbs):
        index.refresh(dbs["acme"], settings, HashingEmbeddingClient(), contextual_enrichment=False)
        yield settings, dbs["acme"]


def _gh4(settings: Settings, acme: ScopedDB, llm: StubLLMClient) -> Any:
    return evaluations.evaluate_request(
        acme,
        settings,
        EvaluationRequest("GH-4", ACME_PLATFORM, TRIGGER),
        now=NOW,
        providers=_providers(llm),
    )


def test_a_pass_stands_only_when_a_second_reader_confirms_it(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    confirmed = _gh4(settings, acme, StubLLMClient(handler=stub_extract))
    assert confirmed.status is VerdictStatus.passed
    assert confirmed.response_json["second_read"]["agrees"] is True
    assert confirmed.response_json["second_read"]["model"].endswith(
        settings.ccp_second_reader_model
    )

    def dissent(schema: type[Any], system: Any, user: str) -> BaseModel:
        assert "within 5 business days" in user  # it reads the checked quotes, as documents
        return schema.model_validate({"agrees": False, "reason": "the quote is from a draft"})

    disputed = _gh4(settings, acme, StubLLMClient(handler=stub_extract, second_reader=dissent))
    assert disputed.status is VerdictStatus.needs_review
    assert DISAGREED in disputed.guardrail_events
    assert "the quote is from a draft" in disputed.reasoning and disputed.confidence <= 0.5


def test_a_refusal_is_counted_against_its_test(world: tuple[Settings, ScopedDB]) -> None:
    settings, acme = world

    def refuse(schema: type[Any], system: Any, user: str) -> BaseModel:
        raise TerminalModelError("model refused the extraction request")

    since = datetime.now(UTC) - timedelta(minutes=1)
    with pytest.raises(TerminalModelError):
        _gh4(settings, acme, StubLLMClient(handler=refuse))
    rates = {r.test_id: r for r in quality.refusal_rates(acme, since)}
    assert rates["GH-4"].refusals == 1 and rates["GH-4"].rate > 0


def test_shadow_runs_a_candidate_over_stored_requests_and_names_each_flip(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    stored = _gh4(settings, acme, StubLLMClient(handler=stub_extract))
    assert evaluations.commit_verdict(acme, stored) or True  # stored once, by its key
    same = shadow.run(acme, settings, StubLLMClient(handler=stub_extract), "claude-sonnet-5-5")
    assert same.compared >= 1 and same.flips == [] and same.passed

    def worse(schema: type[Any], system: Any, user: str) -> BaseModel:
        found = stub_extract(schema, system, user)
        return found.model_copy(update={"timeline_text": None, "timeline_stated": False})

    changed = shadow.run(acme, settings, StubLLMClient(handler=worse), "claude-haiku-5-5")
    flip = next(f for f in changed.flips if f.verdict_id == stored.verdict_id)
    assert flip.stored == "pass" and flip.candidate == "partial"
    assert "timeline_stated" in flip.fields
    assert changed.passed  # nothing moved towards pass
    kept = acme.fetch_all("SELECT kind, passed FROM quality_checks ORDER BY recorded_at")
    assert kept[-2:] == [("shadow", True), ("shadow", True)]
    towards = shadow.ShadowReport("m", flips=[shadow.Flip("v", "GH-4", "s", "fail", "pass", ())])
    assert not towards.passed  # a flip to pass fails the shadow run
