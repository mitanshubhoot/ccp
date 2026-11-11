"""MT4 (A-131): a decision mints one grant, the executor spends it once, and acts only in
the situation that was approved.

The defects: holding an approved proposal was enough to act on it, as often as anyone liked;
nothing recorded what the executor did step by step; an approval given while a failure stood
still acted after the failure was resolved; a proposal could be edited after its approval;
and a reviewer read a model's rationale, not code's account of what would change.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from ccp.approvals import actions as approval_actions
from ccp.approvals import grants
from ccp.approvals import store as approvals
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.verdict import latest_per_subject
from ccp.knowledge.actions import CATALOGUE, REVERSALS, risk_summary
from ccp.remediation import broker, gates, journal, proposals, runner
from ccp.remediation.github_admin import RecordingGitHubAdmin
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus
from tests.support.members import member
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
JANE = "person:hris:person:E1042"
ALLOWED = gates.RemediationConfig(allowed_repos=["acme-example/platform"])
PHRASE = "remove jsmith from acme-example/platform"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_grants", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1",))
        evaluations.drain(acme, settings, now=NOW)
        yield settings.model_copy(update={"ccp_remediation_live": True}), acme


def _proposal(acme: ScopedDB) -> proposals.Proposal:
    verdict = {v.subject: v for v in latest_per_subject(acme, "HR-1")}[JANE]
    return proposals.create(
        acme,
        verdict_id=verdict.verdict_id,
        source="rule",
        action="remove_repo_collaborator",
        target={"repo": "acme-example/platform", "login": "jsmith"},
        rationale="test",
        reversible=True,
        live_capable=True,
    )


def _approve(acme: ScopedDB, settings: Settings, proposal: proposals.Proposal) -> str:
    item = approvals.for_ref(acme, "remediation", proposal.proposal_id)
    assert item is not None and item.is_open
    approval_actions.approve(acme, settings, item.approval_id, member(acme, "ana"))
    return item.approval_id


def _live(
    acme: ScopedDB, settings: Settings, proposal_id: str, github: Any
) -> runner.ExecutionResult:
    return runner.execute(
        acme,
        settings,
        proposal_id,
        live=True,
        confirm=lambda prompt: PHRASE,
        github=github,
        config=ALLOWED,
    )


def _github() -> RecordingGitHubAdmin:
    return RecordingGitHubAdmin(permissions={("acme-example", "platform", "jsmith"): "write"})


def test_the_payload_carries_a_risk_summary_code_wrote(world: tuple[Settings, ScopedDB]) -> None:
    _, acme = world
    proposal = _proposal(acme)
    item = approvals.for_ref(acme, "remediation", proposal.proposal_id)
    assert item is not None
    expected = risk_summary(proposal.action, proposal.target, reversible=True, live_capable=True)
    assert item.payload["risk"] == expected == proposal.risk
    assert expected["changes"] == "jsmith loses direct access to acme-example/platform"
    assert expected["external"] is True and "its own approval" in str(expected["undo"])
    assert item.payload["precondition_sha256"] == proposal.precondition_sha256
    assert proposal.precondition == {
        "subject_key": f"HR-1|{JANE}",
        "status": "fail",
        "access": {"role": "write", "state": "active"},
    }
    assert item.payload_sha256 == proposal.payload_sha256 == proposal.current_hash()
    # a rollback is code's to propose, from what CCP recorded: no model path can name one
    assert all(reversal not in CATALOGUE for reversal in REVERSALS.values())


def test_a_decision_mints_one_grant_and_a_rejection_none(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    approved, rejected = _proposal(acme), _proposal(acme)
    approval_id = _approve(acme, settings, approved)
    grant = grants.for_approval(acme, approval_id)
    assert grant is not None and grant.proposal_id == approved.proposal_id
    assert grant.payload_sha256 == approved.payload_sha256
    assert grant.precondition_sha256 == approved.precondition_sha256
    assert grant.expires_at - grant.issued_at == grants.GRANT_TTL
    assert grants.for_proposal(acme, approved.proposal_id) == [grant]
    item = approvals.for_ref(acme, "remediation", rejected.proposal_id)
    assert item is not None
    approval_actions.reject(acme, settings, item.approval_id, member(acme, "ana"), "not now")
    assert grants.for_proposal(acme, rejected.proposal_id) == []
    assert proposals.get(acme, rejected.proposal_id).status == "rejected"  # type: ignore[union-attr]


def test_the_executor_spends_the_grant_once_and_journals_each_step(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    proposal = _proposal(acme)
    approval_id = _approve(acme, settings, proposal)
    github = _github()
    done = _live(acme, settings, proposal.proposal_id, github)
    assert done.mode == "executed" and len(github.deletes) == 1
    grant = grants.for_approval(acme, approval_id)
    assert grant is not None
    used = grants.use_of(acme, grant.grant_id)
    assert (
        used is not None and used.operation == f"{proposal.proposal_id}:{proposal.payload_sha256}"
    )
    steps = journal.entries(acme, proposal.proposal_id)
    assert [e.entry for e in steps] == ["started", "observed", "finished"]
    assert steps[1].detail == {"permission": "write"} and {e.grant_id for e in steps} == {
        grant.grant_id
    }
    with pytest.raises(grants.GrantUsed), acme.transaction() as cur:  # never twice
        grants.consume(cur, acme.tenant.tenant_id, grant, "another")
    assert _live(acme, settings, proposal.proposal_id, github).detail == "proposal is executed"
    assert len(github.deletes) == 1
    assert not [p for p in runner.audit(acme) if proposal.proposal_id in p]


def test_two_executors_racing_on_one_grant_act_once(
    world: tuple[Settings, ScopedDB], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, acme = world
    proposal = _proposal(acme)
    _approve(acme, settings, proposal)
    both_cleared = threading.Barrier(2, timeout=10)
    real = runner._cleared

    def race(*args: Any) -> Any:  # both have found the grant unspent before either spends it
        found = real(*args)
        both_cleared.wait()
        return found

    monkeypatch.setattr(runner, "_cleared", race)
    github = _github()
    modes: list[str] = []

    def run() -> None:
        modes.append(_live(acme, settings, proposal.proposal_id, github).mode)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(modes) == ["executed", "refused"]
    assert len(github.deletes) == 1
    started = [e for e in journal.entries(acme, proposal.proposal_id) if e.entry == "started"]
    assert len(started) == 1
    # the loser's refusal never reads as the proposal's status, whichever lands last
    assert proposals.get(acme, proposal.proposal_id).status == "executed"  # type: ignore[union-attr]


def test_an_expired_grant_acts_on_nothing_and_asks_again(
    world: tuple[Settings, ScopedDB], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, acme = world
    proposal = _proposal(acme)
    monkeypatch.setattr(grants, "GRANT_TTL", timedelta(microseconds=1))
    _approve(acme, settings, proposal)
    github = _github()
    refused = _live(acme, settings, proposal.proposal_id, github)
    assert refused.mode == "refused" and "expired" in refused.detail
    assert "a new decision is needed" in refused.detail and github.calls == []
    items = approvals.items_for_ref(acme, "remediation", proposal.proposal_id)
    assert [i.state for i in items] == ["approved", "opened"]  # asked again, not acted
    monkeypatch.setattr(grants, "GRANT_TTL", timedelta(hours=24))
    approval_actions.approve(acme, settings, items[1].approval_id, member(acme, "ana"))
    assert _live(acme, settings, proposal.proposal_id, github).mode == "executed"
    assert len(grants.for_proposal(acme, proposal.proposal_id)) == 2


class _Outage(RecordingGitHubAdmin):
    def remove_collaborator(self, org: str, repo: str, login: str) -> dict[str, Any]:
        raise ConnectionError("GitHub is unreachable")


def test_a_failed_call_spends_the_grant_and_a_retry_takes_a_new_decision(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    proposal = _proposal(acme)
    _approve(acme, settings, proposal)
    failed = _live(
        acme,
        settings,
        proposal.proposal_id,
        _Outage(permissions={("acme-example", "platform", "jsmith"): "write"}),
    )
    assert failed.mode == "failed" and failed.proposal.status == "failed"
    assert "ConnectionError" in failed.detail and "asks again" in failed.detail
    steps = journal.entries(acme, proposal.proposal_id)
    assert [e.entry for e in steps] == ["started", "observed", "failed"]
    github = _github()
    blocked = _live(acme, settings, proposal.proposal_id, github)  # the spent grant is no use
    assert blocked.mode == "refused" and "gate5:approval" in blocked.detail and github.calls == []
    items = approvals.items_for_ref(acme, "remediation", proposal.proposal_id)
    assert [i.state for i in items] == ["approved", "opened"]
    approval_actions.approve(acme, settings, items[1].approval_id, member(acme, "ana"))
    assert _live(acme, settings, proposal.proposal_id, github).mode == "executed"


def test_an_approval_does_not_act_once_the_situation_changed(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    proposal = _proposal(acme)
    approval_id = _approve(acme, settings, proposal)
    failing = verdicts.load(acme, proposal.verdict_id)
    assert failing is not None
    resolved = failing.model_copy(  # meanwhile, a re-evaluation found the failure resolved
        update={
            "verdict_id": f"{failing.verdict_id}-resolved",
            "evaluation_key": f"{failing.evaluation_key}-resolved",
            "status": VerdictStatus.passed,
            "created_at": datetime.now(UTC),
        }
    )
    try:
        assert verdicts.persist(acme, resolved)
        github = _github()
        refused = _live(acme, settings, proposal.proposal_id, github)
        assert refused.mode == "refused" and "precondition changed" in refused.detail
        assert "status: 'fail' -> 'pass'" in refused.detail and github.calls == []
        grant = grants.for_approval(acme, approval_id)
        assert grant is not None and grants.use_of(acme, grant.grant_id) is None  # not spent
        assert runner.simulate(acme, settings, proposal.proposal_id).mode == "refused"
    finally:  # later tests in this module read the subject's newest verdict
        with psycopg.connect(settings.database_admin_url, autocommit=True) as owner:
            owner.execute("DELETE FROM verdicts WHERE verdict_id = %s", (resolved.verdict_id,))


def test_records_of_what_happened_cannot_be_rewritten(world: tuple[Settings, ScopedDB]) -> None:
    _, acme = world
    for table in (
        "remediation_proposals",
        "proposal_events",
        "execution_grants",
        "grant_uses",
        "execution_journal",
    ):
        for statement in (f"UPDATE {table} SET tenant_id = tenant_id", f"DELETE FROM {table}"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                acme.execute(statement)


def test_the_broker_takes_only_the_grant_its_approval_minted(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    first, second = _proposal(acme), _proposal(acme)
    first_item = _approve(acme, settings, first)
    second_item = _approve(acme, settings, second)
    other = grants.for_approval(acme, second_item)
    approval = approvals.get(acme, first_item)
    with pytest.raises(broker.NotApproved, match="not minted by"):
        broker.issue(
            acme,
            settings,
            operation=first.proposal_id,
            action="execute",
            approval=approval,
            scope="github:acme-example/platform:remove_collaborator:jsmith",
            grant=other,
        )
