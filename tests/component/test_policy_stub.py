"""The Policy Agent with a stub model: tabled gaps for CTL-VULN-DISCLOSURE per tenant, the
draft file and its approval item, the scrub, and the fixed search argument."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ccp.agent import threads
from ccp.agent.graphs.policy import TRAJECTORY_DRAFT, TRAJECTORY_GAP, scrub_text
from ccp.approvals import store as approvals
from ccp.config import Settings
from ccp.evaluators.tier2 import Providers
from ccp.llm.client import StubLLMClient
from ccp.policy import runner, schema
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants
from tests.support.stub_policy import analyze, analyze_overclaims, compose, gather

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
CONTROL = "CTL-VULN-DISCLOSURE"


def _providers(handler=analyze) -> Providers:  # type: ignore[no-untyped-def]
    return Providers(
        embedder=HashingEmbeddingClient(),
        reranker=None,
        llm=StubLLMClient(handler=handler, chat_handler=gather, complete_handler=compose),
        retrieval=RetrievalConfig(),
    )


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB], Providers]]:
    with prepared_tenants(base_settings, "ccp_policy") as (settings, dbs):
        providers = _providers()
        for db in dbs.values():
            index.refresh(db, settings, providers.embedder, contextual_enrichment=False)
        yield settings, dbs, providers


def test_acme_covers_both_requirements_with_verified_citations(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, providers = world
    report = runner.gap(dbs["acme"], settings, CONTROL, providers, now=NOW)
    assert [g.requirement_id for g in report.gaps] == ["soc2:CC7.1", "iso27001:A.8.8"]
    assert all(g.covered for g in report.gaps) and report.uncovered == ()
    assert all(c.verified for g in report.gaps for c in g.covering_citations)
    assert any("5 business days" in c.quote for c in report.gaps[0].covering_citations)
    assert report.confidence > 0.5 and report.prompt_version == "policy_gap_v1"
    assert "tool_argument_fixed:search_evidence:evidence_type" in report.guardrail_events
    assert schema.latest_report(dbs["acme"], CONTROL) is not None
    row = threads.get(dbs["acme"], report.thread_id or "")
    assert row is not None and tuple(row.trajectory) == TRAJECTORY_GAP


def test_globex_has_the_tabled_gaps(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, providers = world
    report = runner.gap(dbs["globex"], settings, CONTROL, providers, now=NOW)
    assert report.uncovered == ("soc2:CC7.1", "iso27001:A.8.8")
    by_id = {g.requirement_id: g for g in report.gaps}
    assert "timeline" in (by_id["soc2:CC7.1"].gap_description or "")
    assert by_id["soc2:CC7.1"].covering_citations == ()
    assert any(
        e.startswith("injection_marker_detected:") for e in report.guardrail_events
    )  # the SECURITY.md fixture
    assert report.confidence <= 0.60  # the injection cap applies here too


def test_overclaimed_coverage_is_undone_by_verification(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, _ = world
    report = runner.gap(dbs["globex"], settings, CONTROL, _providers(analyze_overclaims), now=NOW)
    assert report.uncovered == ("soc2:CC7.1", "iso27001:A.8.8")
    assert any(e.startswith("gap_uncovered_by_verification:") for e in report.guardrail_events)
    assert any(e.startswith("citation_quote_not_found:") for e in report.guardrail_events)


def test_draft_mode_writes_the_file_opens_an_approval_and_scrubs(world) -> None:  # type: ignore[no-untyped-def]
    settings, dbs, providers = world
    acme = dbs["acme"]
    report, draft = runner.draft(acme, settings, CONTROL, providers, now=NOW)
    path = Path(draft.path)
    assert path.is_file() and path.parent == settings.drafts_dir / "acme"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("## CTL-VULN-DISCLOSURE") and "5 business days" in text
    assert "Globex" not in text and "globex-example" not in text and "ghp_" not in text
    assert "[REDACTED:other_tenant]" in text and "[REDACTED:github_pat_classic]" in text
    assert any(e.startswith("draft_other_tenant_redacted:") for e in draft.scrub_events)
    assert "draft_secret_redacted:github_pat_classic" in draft.scrub_events
    item = approvals.get(acme, draft.approval_id)
    assert item is not None and item.kind == "policy_draft" and item.is_open
    assert item.payload["markdown_sha256"] and item.payload["control_id"] == CONTROL
    row = threads.get(acme, report.thread_id or "")
    assert row is not None and tuple(row.trajectory) == TRAJECTORY_DRAFT
    listed = schema.list_drafts(acme, CONTROL)
    assert listed and listed[0]["draft_id"] == draft.draft_id


def test_scrub_is_case_insensitive_and_reports_counts() -> None:
    cleaned, events = scrub_text(
        "Globex and GLOBEX Corporation; token ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
        ["Globex Corporation", "Globex"],
    )
    assert "Globex" not in cleaned and "GLOBEX" not in cleaned and "ghp_" not in cleaned
    assert cleaned.count("[REDACTED:other_tenant]") == 2
    assert events[0] == "draft_secret_redacted:github_pat_classic"
    assert any(e.startswith("draft_other_tenant_redacted:Globex Corporation:1") for e in events)


def test_other_tenant_tokens_come_from_config_only(world) -> None:  # type: ignore[no-untyped-def]
    settings, _, _ = world
    tokens = runner.other_tenant_tokens(settings, "acme")
    assert "Globex Corporation" in tokens and "globex-example" in tokens and "globex" in tokens
    assert not any("acme" in t.lower() for t in tokens)
