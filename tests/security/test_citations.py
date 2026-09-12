"""S3 (spec.md Layer 13): citations are verified after generation. (a) a fabricated chunk id
is dropped; (b) a real chunk id with a quote not in the chunk is dropped; (c) a verdict whose
only citations are dropped becomes needs_review. All against stubbed model output."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ccp.config import Settings
from ccp.evaluators import citations, tier2
from ccp.evaluators.context import load_context
from ccp.evaluators.extraction import schema_for
from ccp.evaluators.extraction.base import Cited
from ccp.evaluators.extraction.gh4 import DisclosurePolicyFields
from ccp.knowledge import graph as knowledge
from ccp.llm.client import StubLLMClient
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants
from tests.support.stub_extractors import stub_fabricating

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
ACME_PLATFORM = "repo:github:asset:R_kgDOAcmeP001"
CHUNK = "# Security Policy\n\nWe acknowledge every report within 5 business days."
META = schema_for("DisclosurePolicyFields")


def test_a_fabricated_chunk_id_is_dropped() -> None:
    parsed = DisclosurePolicyFields(
        policy_exists=True,
        policy_exists_citation=Cited(chunk_id="fabricated-chunk-id", quote="Security Policy"),
        timeline_stated=False,
    )
    verified = citations.verify_fields(parsed, {"real": CHUNK}, META)
    assert "citation_unknown_chunk:policy_exists:fabricated-chunk-id" in verified.events
    assert verified.values.get("policy_exists") is None
    assert not any(c.chunk_id == "fabricated-chunk-id" and c.verified for c in verified.citations)


def test_a_quote_absent_from_the_chunk_is_dropped() -> None:
    parsed = DisclosurePolicyFields(
        policy_exists=True,
        policy_exists_citation=Cited(chunk_id="real", quote="# Security Policy"),
        timeline_stated=True,
        timeline_stated_citation=Cited(chunk_id="real", quote="we respond within 1 hour"),
        timeline_text="within 1 hour",
        timeline_text_citation=Cited(chunk_id="real", quote="we respond within 1 hour"),
    )
    verified = citations.verify_fields(parsed, {"real": CHUNK}, META)
    assert "citation_quote_not_found:timeline_stated:real" in verified.events
    assert verified.values.get("policy_exists") is True
    assert verified.values.get("timeline_stated") is None
    assert {c.supports for c in verified.citations if c.verified} == {"policy_exists"}


def test_a_dropped_comma_is_tolerated_but_a_different_sentence_is_not() -> None:
    chunk = "We acknowledge every report, within 5 business days, and remediate by severity."
    assert citations.quote_supported(
        "We acknowledge every report within 5 business days and remediate by severity.", chunk
    )
    assert citations.quote_supported("WITHIN 5   business days", chunk)  # case and whitespace
    assert not citations.quote_supported("We never acknowledge reports at all.", chunk)


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, tier2.Providers]]:
    with prepared_tenants(base_settings, "ccp_s3", ("acme",)) as (settings, dbs):
        providers = tier2.Providers(
            embedder=HashingEmbeddingClient(),
            reranker=None,
            llm=StubLLMClient(handler=stub_fabricating),
            retrieval=RetrievalConfig(),
        )
        index.refresh(dbs["acme"], settings, providers.embedder, contextual_enrichment=False)
        yield settings, dbs["acme"], providers


@pytest.mark.db
def test_a_verdict_whose_only_citations_are_dropped_becomes_needs_review(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    spec = knowledge.load().tests["GH-4"]
    ctx = load_context(acme, settings, spec, NOW, NOW)
    verdict = tier2.evaluate(acme, settings, ctx, spec, ACME_PLATFORM, providers)
    assert verdict.status.value == "needs_review"
    assert not any(c.verified for c in verdict.citations)
    kinds = {e.split(":")[0] for e in verdict.guardrail_events}
    assert {"citation_unknown_chunk", "citation_quote_not_found"} <= kinds
    assert verdict.confidence < 0.7
