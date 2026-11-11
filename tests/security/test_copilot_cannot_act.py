"""S11 and S19: "fix this" opens an approval and never executes; actions are validated,
bounded to two per turn, and nothing is written before the explicit yes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ccp.agent import tier3 as _tier3  # noqa: F401 - registers tier 3
from ccp.approvals import actions as approval_actions
from ccp.approvals import store as approvals
from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.copilot import runner
from ccp.evaluators import runner as evaluations
from ccp.evaluators.tier2 import Providers
from ccp.evaluators.verdict import latest_per_subject
from ccp.remediation import github_admin, proposals
from ccp.remediation import runner as remediation
from ccp.retrieval import index
from ccp.storage.scoped import ScopedDB
from tests.component.test_copilot_stub import make_providers
from tests.support.members import member
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, Providers]]:
    with prepared_tenants(base_settings, "ccp_s11", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = make_providers()
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1",))
        evaluations.drain(acme, settings, now=NOW, providers=providers)
        state_writer.drain(acme)
        yield settings, acme, providers


def _proposal_ids(db: ScopedDB, verdict_id: str) -> set[str]:
    return {p.proposal_id for p in proposals.for_verdict(db, verdict_id)}


def _all_proposal_ids(db: ScopedDB) -> set[str]:
    return {
        p.proposal_id
        for v in latest_per_subject(db, "HR-1")
        for p in proposals.for_verdict(db, v.verdict_id)
    }


def _open_ids(db: ScopedDB) -> set[str]:
    return {i.approval_id for i in approvals.list_items(db, "remediation", True)}


def test_fix_this_refuses_the_write_tool_and_opens_an_approval_only_after_yes(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    assert not settings.ccp_remediation_live
    recorder = github_admin.RecordingGitHubAdmin()
    before_proposals = _all_proposal_ids(acme)
    before_open = _open_ids(acme)
    with runner.Session(acme, settings, providers) as session:
        turn = session.ask("Fix HR-1 for Jane Smith.", now=NOW)
        log = (turn.answer.request_json or {}).get("tool_log", [])
        refused = [t for t in log if t.get("tool") == "remove_repo_collaborator"]
        assert refused and all(t.get("is_error") for t in refused)
        assert "tool_not_permitted:remove_repo_collaborator" in turn.answer.guardrail_events
        assert [a["action"] for a in turn.answer.requested_actions] == ["remove_repo_collaborator"]
        assert len(turn.pending) == 1
        verdict_id = turn.pending[0].verdict_id
        # nothing is written by the turn itself: the same rows exist before and after it
        assert _proposal_ids(acme, verdict_id) <= before_proposals
        assert _open_ids(acme) == before_open
        opened = session.confirm(turn, now=NOW)
    assert len(opened.approval_ids) == 1
    item = approvals.get(acme, opened.approval_ids[0])
    assert item is not None and item.kind == "remediation" and item.is_open
    proposal_id = opened.proposal_ids[0]
    result = remediation.execute(acme, settings, proposal_id, live=False, github=recorder)
    assert result.mode == "dry_run" and recorder.deletes == []
    # approved, but CCP_REMEDIATION_LIVE unset: still only a dry run
    approval_actions.approve(acme, settings, item.approval_id, member(acme, "cto"), "go")
    result = remediation.execute(acme, settings, proposal_id, live=False, github=recorder)
    assert result.mode == "dry_run" and recorder.deletes == []
    result = remediation.execute(acme, settings, proposal_id, live=True, github=recorder)
    assert result.mode == "refused" and recorder.deletes == []


def test_three_requested_actions_yield_two_approvals_and_a_note(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    with runner.Session(acme, settings, providers) as session:
        turn = session.ask("Fix everything for Jane Smith.", now=NOW)
        assert len(turn.answer.requested_actions) == 2 and len(turn.pending) == 2
        assert "requested_actions_capped:1" in turn.answer.guardrail_events
        assert "at most 2 per turn" in turn.answer.text
        opened = session.confirm(turn, now=NOW)
    assert len(set(opened.approval_ids)) == 2
    items = [approvals.get(acme, aid) for aid in opened.approval_ids]
    assert all(i is not None and i.kind == "remediation" for i in items)
    # the repo-collaborator item was reused from the first test (already approved there);
    # the org-membership item is new and open
    assert any(i is not None and i.is_open for i in items)


def test_an_action_on_another_tenants_repo_is_rejected_and_opens_nothing(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, providers = world
    from ccp.evaluators.extraction.investigation import ProposalTarget

    llm = providers.llm
    base = llm.handler

    def extract(schema, system, user):  # type: ignore[no-untyped-def]
        out = base(schema, system, user)
        if schema.__name__ == "CopilotAnswerDraft" and out.requested_actions:
            out.requested_actions[0].target = ProposalTarget(
                repo="globex-example/platform", login="jsmith"
            )
        return out

    llm.handler = extract
    try:
        before = _all_proposal_ids(acme)
        with runner.Session(acme, settings, providers) as session:
            turn = session.ask("Fix HR-1 for Jane Smith.", now=NOW)
            assert turn.answer.requested_actions and turn.pending == ()
            assert turn.rejected and any("unknown_repo" in e for e in turn.rejected)
            assert set(turn.rejected) <= set(
                turn.answer.guardrail_events
            )  # durable, not only logged
            opened = session.confirm(turn, now=NOW)
        assert opened.approval_ids == () and opened.proposal_ids == ()
        assert _all_proposal_ids(acme) == before
    finally:
        llm.handler = base
