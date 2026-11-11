"""S8: remediation cannot act without all five gates. Parametrized over the 31 combinations
where at least one gate is missing; the GitHub client's delete is never called and the
proposal row records the refusal. The all-gates case makes exactly one call; an edited
proposal fails the payload-hash check."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ccp.approvals import store as approvals
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators.verdict import latest_per_subject
from ccp.remediation import gates, proposals, runner
from ccp.remediation.github_admin import RecordingGitHubAdmin
from ccp.storage.scoped import ScopedDB
from tests.support.members import member
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
JANE = "person:hris:person:E1042"
ALLOWED = gates.RemediationConfig(
    allowed_repos=["acme-example/platform"], protected_logins=["octocat"]
)
GATES = ("gate1", "gate2", "gate3", "gate4", "gate5")


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, str]]:
    with prepared_tenants(base_settings, "ccp_gates", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1",))
        evaluations.drain(acme, settings, now=NOW)
        verdict = {v.subject: v for v in latest_per_subject(acme, "HR-1")}[JANE]
        summary = runner.propose(acme, verdict)
        repo_proposal = next(p for p in summary.proposals if p.action == "remove_repo_collaborator")
        yield settings, acme, repo_proposal.proposal_id


def _fresh_proposal(acme: ScopedDB, settings: Settings) -> str:
    """Every combination needs an un-refused proposal; clone the verdict's repo proposal."""
    verdict = {v.subject: v for v in latest_per_subject(acme, "HR-1")}[JANE]
    created = proposals.create(
        acme,
        verdict_id=verdict.verdict_id,
        source="rule",
        action="remove_repo_collaborator",
        target={"repo": "acme-example/platform", "login": "jsmith"},
        rationale="test",
        reversible=True,
        live_capable=True,
    )
    return created.proposal_id


@pytest.mark.parametrize(
    "present",
    [c for n in range(5) for c in itertools.combinations(GATES, n)],
    ids=lambda c: "+".join(c) or "none",
)
def test_any_missing_gate_refuses_and_never_calls_github(
    world: tuple[Settings, ScopedDB, str], present: tuple[str, ...]
) -> None:
    settings, acme, _ = world
    proposal_id = _fresh_proposal(acme, settings)
    live_settings = settings.model_copy(update={"ccp_remediation_live": "gate1" in present})
    if "gate5" in present:
        item = approvals.for_ref(acme, "remediation", proposal_id)
        assert item is not None
        approvals.record_event(acme, item.approval_id, "approved", member(acme, "reviewer"))
    config = ALLOWED if "gate3" in present else gates.RemediationConfig()
    phrase = gates.confirmation_phrase("acme-example", "platform", "jsmith")
    github = RecordingGitHubAdmin(permissions={("acme-example", "platform", "jsmith"): "write"})
    result = runner.execute(
        acme,
        live_settings,
        proposal_id,
        live="gate2" in present,
        confirm=(lambda prompt: phrase) if "gate4" in present else (lambda prompt: "no"),
        github=github,
        config=config,
    )
    if "gate2" not in present:
        assert result.mode == "dry_run" and result.proposal.status == "dry_run"
        assert result.proposal.planned_call == {
            "method": "DELETE",
            "path": "/repos/acme-example/platform/collaborators/jsmith",
            "body": None,
        }
    else:
        assert result.mode == "refused" and result.proposal.status == "refused"
        missing = set(GATES) - set(present)
        assert result.proposal.refusal is not None
        for gate in missing:
            assert gate in result.proposal.refusal
    assert github.deletes == [] and github.calls == []


def test_all_five_gates_make_exactly_one_call_and_record_the_pre_state(
    world: tuple[Settings, ScopedDB, str],
) -> None:
    settings, acme, proposal_id = world
    item = approvals.for_ref(acme, "remediation", proposal_id)
    assert item is not None
    approvals.record_event(
        acme, item.approval_id, "approved", member(acme, "security-lead"), "confirmed"
    )
    github = RecordingGitHubAdmin(permissions={("acme-example", "platform", "jsmith"): "write"})
    result = runner.execute(
        acme,
        settings.model_copy(update={"ccp_remediation_live": True}),
        proposal_id,
        live=True,
        confirm=lambda prompt: "remove jsmith from acme-example/platform",
        github=github,
        config=ALLOWED,
    )
    assert result.mode == "executed" and result.proposal.status == "executed"
    assert github.deletes == [
        {"method": "DELETE", "org": "acme-example", "repo": "platform", "login": "jsmith"}
    ]
    assert result.proposal.pre_state == {"permission": "write"}
    assert result.proposal.executed_call is not None and result.proposal.executed_call[
        "result"
    ] == {"status_code": 204}
    # a rollback is an action too: its own approval first, then all five gates when live
    live_settings = settings.model_copy(update={"ccp_remediation_live": True})
    restore = "restore jsmith to acme-example/platform"
    calls_before = len(github.calls)
    first = runner.rollback(
        acme,
        live_settings,
        proposal_id,
        live=True,
        confirm=lambda p: restore,
        github=github,
        config=ALLOWED,
    )
    assert first.mode == "refused" and "gate5:approval" in first.detail
    assert len(github.calls) == calls_before
    undo = approvals.for_ref(acme, "remediation_rollback", proposal_id)
    assert undo is not None and undo.is_open and undo.payload["restore_permission"] == "write"
    approvals.record_event(
        acme, undo.approval_id, "approved", member(acme, "security-lead"), "undo it"
    )
    dry = runner.rollback(acme, live_settings, proposal_id, github=github, config=ALLOWED)
    assert dry.mode == "dry_run" and len(github.calls) == calls_before
    wrong = runner.rollback(
        acme,
        live_settings,
        proposal_id,
        live=True,
        confirm=lambda p: "remove jsmith from acme-example/platform",
        github=github,
        config=ALLOWED,
    )
    assert wrong.mode == "refused" and "gate4:confirmation" in wrong.detail
    assert len(github.calls) == calls_before
    rolled = runner.rollback(
        acme,
        live_settings,
        proposal_id,
        live=True,
        confirm=lambda p: restore,
        github=github,
        config=ALLOWED,
    )
    assert (
        rolled.mode == "rolled_back"
        and github.calls[-1]["method"] == "PUT"
        and github.calls[-1]["permission"] == "write"
    )
    assert runner.audit(acme) == []


def test_an_edited_proposal_fails_the_payload_hash_check(
    world: tuple[Settings, ScopedDB, str],
) -> None:
    settings, acme, _ = world
    proposal_id = _fresh_proposal(acme, settings)
    item = approvals.for_ref(acme, "remediation", proposal_id)
    assert item is not None
    approvals.record_event(acme, item.approval_id, "approved", member(acme, "reviewer"))
    acme.execute(
        "UPDATE remediation_proposals SET target = %s WHERE proposal_id = %s",
        ('{"repo": "acme-example/platform", "login": "pnatarajan"}', proposal_id),
    )
    github = RecordingGitHubAdmin()
    result = runner.execute(
        acme,
        settings.model_copy(update={"ccp_remediation_live": True}),
        proposal_id,
        live=True,
        confirm=lambda prompt: "remove pnatarajan from acme-example/platform",
        github=github,
        config=ALLOWED,
    )
    assert result.mode == "refused" and "gate5:approval" in (result.proposal.refusal or "")
    assert github.calls == []


def test_org_member_removal_is_never_live(world: tuple[Settings, ScopedDB, str]) -> None:
    settings, acme, _ = world
    verdict = {v.subject: v for v in latest_per_subject(acme, "HR-1")}[JANE]
    org_proposal = next(
        p
        for p in proposals.for_verdict(acme, verdict.verdict_id)
        if p.action == "remove_org_member"
    )
    assert not org_proposal.live_capable and org_proposal.reversible
    dry = runner.execute(acme, settings, org_proposal.proposal_id)
    assert dry.mode == "dry_run" and dry.proposal.planned_call == {
        "method": "DELETE",
        "path": "/orgs/acme-example/members/jsmith",
        "body": None,
    }
    item = approvals.for_ref(acme, "remediation", org_proposal.proposal_id)
    assert item is not None
    approvals.record_event(acme, item.approval_id, "approved", member(acme, "reviewer"))
    github = RecordingGitHubAdmin()
    live = runner.execute(
        acme,
        settings.model_copy(update={"ccp_remediation_live": True}),
        org_proposal.proposal_id,
        live=True,
        confirm=lambda prompt: "x",
        github=github,
        config=ALLOWED,
    )
    assert (
        live.mode == "refused"
        and "not live-capable" in (live.proposal.refusal or "")
        and github.calls == []
    )
