"""The Investigator with a stub model: the four tabled investigations (category, missing
evidence, actions), the verdicts-topic consumer, and the loop from verdict to approval."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ccp.agent import threads
from ccp.agent import tier3 as _tier3  # noqa: F401 - registers tier 3 for HR-4
from ccp.agent.graphs.investigator import TRAJECTORY
from ccp.approvals import store as approvals
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators.rule_versions import rule_stamp
from ccp.evaluators.tier2 import Providers
from ccp.evaluators.verdict import Verdict, evaluation_key, latest_per_subject, persist
from ccp.investigation import runner, schema
from ccp.knowledge import graph as knowledge
from ccp.llm.client import StubLLMClient
from ccp.remediation import proposals, suggestions
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus
from tests.support.pipeline import prepared_tenants
from tests.support.stub_agent import stub_chat_search_once, stub_extract_agent
from tests.support.stub_investigator import diagnose, gather_once

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
JANE = "person:hris:person:E1042"


class _Stub(StubLLMClient):
    """Routes extraction calls by schema: tier 3 schemas to the tier 3 stub, the draft to the
    investigator stub; chat calls by the prompt: the fallback searcher versus the gatherer."""

    def __init__(self) -> None:
        super().__init__()

        def extract(schema, system, user):  # type: ignore[no-untyped-def]
            if schema.__name__ == "InvestigationDraft":
                return diagnose(schema, system, user)
            return stub_extract_agent(schema, system, user)

        def chat(system, messages, tools):  # type: ignore[no-untyped-def]
            if "investigator" in system[0].text.lower():
                return gather_once(system, messages, tools)
            return stub_chat_search_once(system, messages, tools)

        self.handler = extract
        self.chat_handler = chat


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, Providers]]:
    with prepared_tenants(base_settings, "ccp_inv", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = Providers(
            embedder=HashingEmbeddingClient(),
            reranker=None,
            llm=_Stub(),
            retrieval=RetrievalConfig(),
        )
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1", "HR-5", "HR-4"))
        evaluations.drain(acme, settings, now=NOW, providers=providers)
        yield settings, acme, providers


def _verdict(acme: ScopedDB, test_id: str, subject: str) -> Verdict:
    return {v.subject: v for v in latest_per_subject(acme, test_id)}[subject]


def test_jane_hr1_is_an_offboarding_gap_with_the_tabled_actions(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    verdict = _verdict(acme, "HR-1", JANE)
    inv = runner.investigate(acme, settings, verdict, providers, now=NOW)
    assert inv.root_cause_category == "offboarding_gap" and inv.missing_evidence == ()
    assert [(p.action, p.target) for p in inv.proposed_actions] == [
        ("remove_org_member", {"org": "acme-example", "login": "jsmith"}),
        ("remove_repo_collaborator", {"repo": "acme-example/platform", "login": "jsmith"}),
        ("notify_owner", {"subject_key": "HR-1|person:hris:person:E1042"}),
    ]
    assert [p.live_capable for p in inv.proposed_actions] == [False, True, False]
    assert all(p.proposal_id for p in inv.proposed_actions)
    rows = proposals.for_verdict(acme, verdict.verdict_id)
    assert {r.source for r in rows} == {"investigator"} and len(rows) == 3
    for r in rows:
        item = approvals.for_ref(acme, "remediation", r.proposal_id)
        assert item is not None and item.is_open
    assert inv.thread_id and inv.thread_id.startswith("acme:investigate:")
    row = threads.get(acme, inv.thread_id)
    assert row is not None and row.status == "finished" and tuple(row.trajectory) == TRAJECTORY
    assert schema.for_verdict(acme, verdict.verdict_id) is not None
    (fix,) = suggestions.for_verdict(acme, verdict.verdict_id)  # the model's choice (A-160)
    assert (fix.fix, fix.source, fix.resource) == ("github_remove_collaborator", "model", "jsmith")
    assert fix.investigation_id == inv.investigation_id and fix.status == "open"
    assert fix.body["cli"] == [
        "gh api --method DELETE repos/acme-example/platform/collaborators/jsmith"
    ]
    again = runner.investigate(acme, settings, verdict, providers, now=NOW)
    assert again.investigation_id == inv.investigation_id  # idempotent per verdict


def test_hr4_partial_names_the_missing_review_and_requests_evidence(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    verdict = _verdict(acme, "HR-4", "tenant")
    assert verdict.status is VerdictStatus.partial
    inv = runner.investigate(acme, settings, verdict, providers, now=NOW)
    assert inv.root_cause_category == "missing_evidence" and "Payroll-SaaS" in inv.root_cause
    assert [m.evidence_type.value for m in inv.missing_evidence] == ["access_review_report"]
    assert [p.action for p in inv.proposed_actions] == ["request_evidence"]
    request = approvals.for_ref(acme, "evidence_request", inv.investigation_id)
    assert request is not None and request.is_open
    assert request.payload["missing_evidence"][0]["evidence_type"] == "access_review_report"


def test_hr5_unowned_account_is_ownership_with_a_notification_only(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    inv = runner.investigate(acme, settings, _verdict(acme, "HR-5", "tenant"), providers, now=NOW)
    assert inv.root_cause_category == "ownership" and "ops-shared" in inv.root_cause
    assert [p.action for p in inv.proposed_actions] == [
        "notify_owner"
    ] and inv.missing_evidence == ()


def test_needs_review_from_a_queued_identity_link_is_identity_unresolved(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    queued = Verdict(
        evaluation_key=evaluation_key(
            "acme",
            "HR-1",
            "HR-1|person:hris:person:E1017",
            NOW,
            None,
            None,
            rule=rule_stamp(knowledge.load().tests["HR-1"], acme.tenant.settings),
        ),
        tenant_id=acme.tenant.tenant_id,
        test_id="HR-1",
        subject_key="HR-1|person:hris:person:E1017",
        status=VerdictStatus.needs_review,
        reasoning="Dana Okafor is terminated but an identity link is awaiting human review",
        evidence_refs=("hris:person:E1017",),
        confidence=1.0,
        confidence_cap=1.0,
        tier=1,
        guardrail_events=("identity_pending_review",),
        trigger_observed_at=NOW,
        valid_from=NOW,
        created_at=NOW,
    )
    assert persist(acme, queued)
    inv = runner.investigate(acme, settings, queued, providers, now=NOW)
    assert inv.root_cause_category == "identity_unresolved"
    assert [(p.action, p.target) for p in inv.proposed_actions] == [
        ("notify_owner", {"subject_key": "HR-1|person:hris:person:E1017"})
    ]


def test_the_consumer_investigates_only_fail_and_needs_review_and_parks_without_a_key(
    world,
) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    keyless = runner.drain(
        acme,
        settings,
        Providers(
            embedder=HashingEmbeddingClient(), reranker=None, llm=None, retrieval=RetrievalConfig()
        ),
    )
    # the consumer reads every verdict and queues one job per uninvestigated fail or
    # needs_review verdict; the partial HR-4 verdict and the two investigated above are
    # skipped; without a model key every queued job parks (A-109)
    assert keyless.consumed >= 3 and keyless.skipped >= 3 and keyless.investigated == 0
    assert keyless.parked == keyless.enqueued
    summary = runner.drain(acme, settings, providers)
    assert summary.consumed == 0  # the cursor advanced
    assert summary.investigated == keyless.enqueued  # the parked jobs ran once a key was set
    again = runner.drain(acme, settings, providers)
    assert again.consumed == 0 and again.investigated == 0


def test_investigator_never_reads_another_tenant_or_writes_state(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    inv = schema.for_verdict(acme, _verdict(acme, "HR-1", JANE).verdict_id)
    assert inv is not None
    assert all(c.chunk_id for c in inv.citations)
    assert acme.fetch_one("SELECT count(*) FROM investigations WHERE tenant_id <> 'acme'") == (0,)
    assert knowledge.load().tests["HR-1"].tier == 1  # the investigation never re-evaluated anything
