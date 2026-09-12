"""The full loop in simulated mode (spec.md Layer 10 "Re-evaluate"): evaluate, expect HR-1 fail
for Jane Smith, approve the proposals, execute in simulated mode, re-pull through the
test-only fixture overlay, and assert compliance state now shows a pass row superseding the
fail row with reason evaluation. The rejected-approval variant leaves state unchanged."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ccp.approvals import actions as approval_actions
from ccp.approvals import store as approvals
from ccp.compliance_state import reader, run
from ccp.config import Settings
from ccp.connectors.github import fixture as github_fixture
from ccp.evaluators.tier2 import Providers
from ccp.evaluators.verdict import latest_per_subject
from ccp.remediation import proposals, reevaluate, runner
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from ccp.types import VerdictStatus

pytestmark = pytest.mark.db

JANE = "HR-1|person:hris:person:E1042"
JANE_GH1 = "GH-1|person:hris:person:E1042"
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
# the simulated removal must lie in the past for current_state (as of the wall clock) to see it
FIXED_AT = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)
KEYLESS = Providers(
    embedder=HashingEmbeddingClient(), reranker=None, llm=None, retrieval=RetrievalConfig()
)


@pytest.fixture()
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with scratch_database(base_settings, prefix="ccp_loop") as settings:
            pool = app_pool(settings, max_size=4)
            try:
                acme = ScopedDB(pool, TenantContext.for_id(settings, "acme"))
                run.run(acme, settings, providers=KEYLESS, now=NOW)
                yield settings, acme
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover
        pytest.skip(f"Postgres not reachable: {exc}")


def _jane_verdict(acme: ScopedDB):  # type: ignore[no-untyped-def]
    return {v.subject_key: v for v in latest_per_subject(acme, "HR-1")}[JANE]


def test_simulated_loop_closes_the_finding(world: tuple[Settings, ScopedDB]) -> None:
    settings, acme = world
    before = reader.current_state(acme, JANE)
    assert before is not None and before.status is VerdictStatus.failed
    summary = runner.propose(acme, _jane_verdict(acme))
    removable = [
        p
        for p in summary.proposals
        if p.action in ("remove_repo_collaborator", "remove_org_member")
    ]
    assert len(removable) == 2
    for p in removable:  # a human approves each proposal in the shared stage
        item = approvals.for_ref(acme, "remediation", p.proposal_id)
        assert item is not None and item.is_open
        approval_actions.approve(
            acme, settings, item.approval_id, "security-lead", "approved for the drill"
        )
    # execute in simulated mode: nothing real exists to change, gate 5 still applies
    connector = github_fixture.GitHubFixtureConnector(settings.ccp_fixtures_dir)
    for n, p in enumerate(removable):
        result = runner.simulate(acme, settings, p.proposal_id)
        assert result.mode == "simulated", result.detail
        connector = github_fixture.apply_proposal(
            connector, acme.tenant, p.action, p.target, FIXED_AT, f"sim-{n}"
        )
    # re-pull through the overlay: the loop closes through the same topics as any change
    outcome = reevaluate.reevaluate(acme, settings, connector, KEYLESS)
    assert outcome.landed >= 3 and outcome.enqueued >= 2 and outcome.evaluated >= 2
    after = reader.current_state(acme, JANE)
    assert after is not None and after.status is VerdictStatus.passed
    assert after.reason == "evaluation" and after.supersedes_row_id == before.row_id
    assert after.valid_from == FIXED_AT  # the exposure closed when the last grant was revoked
    verdict = _jane_verdict(acme)
    assert "revoked after the SLA deadline" in verdict.reasoning
    assert any(e.startswith("revoked_after_sla:") for e in verdict.guardrail_events)
    gh1 = reader.current_state(acme, JANE_GH1)
    assert gh1 is not None and gh1.status is VerdictStatus.passed
    chain = reader.chain(acme, JANE)
    assert [r.status.value for r in chain] == ["fail", "pass"]
    # the fail row now has a closed interval: the audit trail shows how long the gap lasted
    assert reader.state_as_of(acme, JANE, valid_at=NOW, known_at=datetime.now(UTC)) is not None
    assert (
        reader.state_as_of(acme, JANE, valid_at=NOW, known_at=datetime.now(UTC)).status
        is VerdictStatus.failed
    )  # type: ignore[union-attr]
    assert {
        p.status
        for p in proposals.for_verdict(acme, before.verdict_id)
        if p.action != "notify_owner"
    } == {"simulated"}


def test_rejected_approval_leaves_state_unchanged(world: tuple[Settings, ScopedDB]) -> None:
    settings, acme = world
    before = reader.current_state(acme, JANE)
    assert before is not None
    summary = runner.propose(acme, _jane_verdict(acme))
    repo = next(p for p in summary.proposals if p.action == "remove_repo_collaborator")
    item = approvals.for_ref(acme, "remediation", repo.proposal_id)
    assert item is not None
    outcome = approval_actions.reject(
        acme, settings, item.approval_id, "security-lead", "not this one"
    )
    assert outcome.approval.state == "rejected"
    assert proposals.get(acme, repo.proposal_id).status == "rejected"  # type: ignore[union-attr]
    refused = runner.simulate(acme, settings, repo.proposal_id)
    assert refused.mode == "refused"
    with pytest.raises(ValueError):  # a decided item cannot be approved afterwards
        approval_actions.approve(acme, settings, item.approval_id, "someone")
    after = reader.current_state(acme, JANE)
    assert (
        after is not None and after.row_id == before.row_id and after.status is VerdictStatus.failed
    )
