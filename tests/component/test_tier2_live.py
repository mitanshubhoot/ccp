"""Live tier 2 against the real providers. Skipped without ANTHROPIC_API_KEY (and uses the
local embedder unless VOYAGE_API_KEY is set). These are the keyed half of the Phase 7b
done-when; they spend a few cents."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ccp.config import Settings
from ccp.evaluators import tier2
from ccp.evaluators.context import load_context
from ccp.knowledge import graph as knowledge
from ccp.retrieval import index
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus
from tests.support.pipeline import prepared_tenants

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"),
]

NOW = datetime.now(UTC)


@pytest.fixture(scope="module")
def world(
    base_settings: Settings,
) -> Iterator[tuple[Settings, dict[str, ScopedDB], tier2.Providers]]:
    with prepared_tenants(base_settings, "ccp_live") as (settings, dbs):
        providers = tier2.Providers.from_settings(settings)
        for db in dbs.values():
            index.refresh(
                db, settings, providers.embedder, providers.llm, contextual_enrichment=False
            )
        yield settings, dbs, providers


def _evaluate(world, tenant: str, test_id: str, subject: str):  # type: ignore[no-untyped-def]
    settings, dbs, providers = world
    spec = knowledge.load().tests[test_id]
    ctx = load_context(dbs[tenant], settings, spec, NOW, NOW)
    return tier2.evaluate(dbs[tenant], settings, ctx, spec, subject, providers)


def test_live_gh4_acme_passes_and_globex_injection_is_ignored(world) -> None:  # type: ignore[no-untyped-def]
    acme = _evaluate(world, "acme", "GH-4", "repo:github:asset:R_kgDOAcmeP001")
    assert acme.status is VerdictStatus.passed, acme.reasoning
    assert any(c.verified for c in acme.citations)
    globex = _evaluate(world, "globex", "GH-4", "repo:github:asset:R_kgDOGlobexP001")
    assert globex.status is not VerdictStatus.passed
    assert globex.confidence <= 0.60 and any(
        e.startswith("injection_marker_detected") for e in globex.guardrail_events
    )


def test_live_hr3_pdf_backed_case_passes(world) -> None:  # type: ignore[no-untyped-def]
    v = _evaluate(world, "acme", "HR-3", "person:hris:person:E1058")
    assert v.status is VerdictStatus.passed, v.reasoning
    mismatch = _evaluate(world, "acme", "HR-3", "person:hris:person:E1050")
    assert mismatch.status is VerdictStatus.failed, mismatch.reasoning


def test_live_second_identical_call_reads_the_prompt_cache_when_the_prefix_is_long_enough(
    world,
) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, providers = world
    first = _evaluate(world, "acme", "GH-4", "repo:github:asset:R_kgDOAcmeP001")
    second = _evaluate(world, "acme", "GH-4", "repo:github:asset:R_kgDOAcmeP001")
    assert first.response_json and second.response_json
    usage = second.response_json.get("usage", {})
    prefix_tokens = int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0))
    minimum = 4096 if "haiku-4-5" in settings.ccp_tier2_model else 1024
    if prefix_tokens < minimum:
        pytest.skip(
            f"prompt prefix below the {minimum}-token cache minimum for {settings.ccp_tier2_model}"
        )
    assert int(usage.get("cache_read_input_tokens", 0)) > 0
