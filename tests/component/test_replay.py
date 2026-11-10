"""Replay (spec.md Layer 12): every model-backed record of a stubbed run rebuilds to an empty
request diff; re-execution against the stub reproduces the response; a changed index shows
up as a non-empty diff."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ccp.agent import tier3 as _tier3  # noqa: F401 - registers tier 3
from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.copilot import runner as copilot
from ccp.evaluators import runner as evaluations
from ccp.evaluators.tier2 import Providers
from ccp.evaluators.verdict import latest_per_subject
from ccp.investigation import runner as investigator
from ccp.policy import runner as policy
from ccp.replay import rebuild as replay
from ccp.replay.rebuild import user_text
from ccp.retrieval import index
from ccp.storage.scoped import ScopedDB
from tests.component.test_copilot_stub import Stub
from tests.support.pipeline import prepared_tenants
from tests.support.stub_policy import analyze as policy_analyze
from tests.support.stub_policy import compose as policy_compose
from tests.support.stub_policy import gather as policy_gather

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
JANE = "person:hris:person:E1042"


class FullStub(Stub):
    """The copilot/investigator/tier stub plus the policy agent's calls."""

    def __init__(self) -> None:
        super().__init__()
        extract = self.handler
        chat = self.chat_handler

        def extract_all(schema, system, user):  # type: ignore[no-untyped-def]
            if schema.__name__ == "GapReportDraft":
                return policy_analyze(schema, system, user)
            assert extract is not None
            return extract(schema, system, user)

        def chat_all(system, messages, tools):  # type: ignore[no-untyped-def]
            if "policy" in system[0].text.lower() and "gap" in system[0].text.lower():
                return policy_gather(system, messages, tools)
            assert chat is not None
            return chat(system, messages, tools)

        self.handler = extract_all
        self.chat_handler = chat_all
        self.complete_handler = policy_compose


@pytest.fixture(scope="module")
def world(
    base_settings: Settings,
) -> Iterator[tuple[Settings, ScopedDB, Providers, dict[str, str]]]:
    with prepared_tenants(base_settings, "ccp_replay", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        from ccp.retrieval.config import RetrievalConfig
        from ccp.retrieval.embeddings import HashingEmbeddingClient

        providers = Providers(
            embedder=HashingEmbeddingClient(),
            reranker=None,
            llm=FullStub(),
            retrieval=RetrievalConfig(),
        )
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1", "GH-4"))
        evaluations.drain(acme, settings, now=NOW, providers=providers)
        state_writer.drain(acme)
        gh4 = latest_per_subject(acme, "GH-4")[0]
        hr1 = {v.subject: v for v in latest_per_subject(acme, "HR-1")}[JANE]
        inv = investigator.investigate(acme, settings, hr1, providers, now=NOW)
        report = policy.gap(acme, settings, "CTL-VULN-DISCLOSURE", providers, now=NOW)
        with copilot.Session(acme, settings, providers) as session:
            turn = session.ask("Why did GH-4 pass for the platform repository?", now=NOW)
        ids = {
            "verdict": gh4.verdict_id,
            "tier1": hr1.verdict_id,
            "investigation": inv.investigation_id,
            "gap_report": report.report_id,
            "copilot_answer": turn.answer.answer_id,
        }
        yield settings, acme, providers, ids


def test_every_model_backed_record_rebuilds_to_an_empty_diff(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, _, ids = world
    for kind in ("verdict", "investigation", "gap_report", "copilot_answer"):
        report = replay.rebuild(acme, settings, ids[kind])
        assert report.kind == kind, kind
        assert report.calls, kind
        assert report.diff == [], (kind, report.diff)
        assert "request diff: empty" in report.format()
    tier2 = replay.rebuild(acme, settings, ids["verdict"])
    assert tier2.fence_nonce and tier2.calls[0].fenced_chunks >= 1
    assert tier2.calls[0].schema == "DisclosurePolicyFields"


def test_tier1_and_unknown_records(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, _, ids = world
    assert replay.rebuild(acme, settings, ids["tier1"]).calls == ()
    with pytest.raises(LookupError):
        replay.rebuild(acme, settings, "no-such-record")


def test_re_execution_against_the_stub_reproduces_the_response(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers, ids = world
    assert providers.llm is not None
    results = replay.execute(acme, settings, ids["verdict"], providers.llm)
    assert results == [("extract", [])]


def test_a_changed_document_is_drift_and_the_replay_still_passes(world) -> None:  # type: ignore[no-untyped-def]
    """A-101: replay works from the fenced text stored with the request; an index that has
    changed since is reported as drift, and the stored call re-executes unchanged."""
    settings, acme, _, ids = world
    report = replay.rebuild(acme, settings, ids["verdict"])
    chunk_ids = [
        line.split("chunk_id=")[1].split(" ")[0]
        for line in str(user_text(report.calls[0].request) or "").splitlines()
        if line.startswith("<<EVIDENCE-")
    ]
    assert chunk_ids
    acme.execute(
        "UPDATE chunks SET text = text || ' (edited)' WHERE chunk_id = %s", (chunk_ids[0],)
    )
    try:
        changed = replay.rebuild(acme, settings, ids["verdict"])
        assert changed.diff == []
        drift = [line for call in changed.calls for line in call.drift]
        assert any("fenced text differs" in line for line in drift)
        assert "index changed since the call" in changed.format()
        llm = world[2].llm
        assert replay.execute(acme, settings, ids["verdict"], llm) == [("extract", [])]
    finally:
        acme.execute(
            "UPDATE chunks SET text = left(text, length(text) - 9) WHERE chunk_id = %s",
            (chunk_ids[0],),
        )
    assert replay.rebuild(acme, settings, ids["verdict"]).diff == []
