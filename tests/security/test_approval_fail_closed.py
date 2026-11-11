"""NT2: approvals fail closed (spec.md Layer 16, A-95).

- No decision other than a well-formed one from a named reviewer changes a draft; an unknown
  action, a missing reviewer, or a stray status holds it as needs_review (property tests).
- Items other than tier 3 interrupts take a plain approve only (property test).
- A tier 3 decision is recorded before the graph resumes and survives a failed resume or a
  failed commit; `ccp approvals resume` applies it later, once.
- An item whose payload is not the interrupt the thread waits on is refused, with nothing
  recorded; a decision stored without a reviewer is held as needs_review by the graph.
- A rollback needs its own approval, bound to the permission it restores.
- The audits find actions without approvals and decisions without actions.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st
from ulid import ULID

from ccp.agent import decisions, threads, tier3
from ccp.agent.graphs.tier3 import NO_DECISION, apply_decision
from ccp.approvals import actions, store
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.context import load_context
from ccp.evaluators.verdict import latest_per_subject
from ccp.knowledge import graph as knowledge
from ccp.llm.client import StubLLMClient
from ccp.remediation import gates, proposals, runner
from ccp.remediation.github_admin import RecordingGitHubAdmin
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus
from tests.support.members import member
from tests.support.pipeline import prepared_tenants
from tests.support.stub_agent import stub_chat_search_once, stub_extract_agent

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
JANE = "person:hris:person:E1042"
STATUSES = {s.value for s in VerdictStatus}
ALLOWED = gates.RemediationConfig(allowed_repos=["acme-example/platform"])


# -- pure: the decision rules ----------------------------------------------------------------


def _valid(action: Any, reviewer: Any, status: Any) -> bool:
    named = isinstance(reviewer, str) and bool(reviewer.strip())
    if action == "override":
        return named and status in STATUSES
    return action in ("approve", "regather") and named and status is None


@hsettings(max_examples=300, deadline=None)
@given(
    action=st.one_of(
        st.none(),
        st.integers(),
        st.sampled_from(["approve", "override", "regather", "APPROVE", "approve ", "auto"]),
        st.text(max_size=12),
    ),
    reviewer=st.one_of(st.none(), st.just(""), st.just("   "), st.text(max_size=8)),
    status=st.one_of(st.none(), st.sampled_from(sorted(STATUSES)), st.text(max_size=8)),
)
def test_the_gate_changes_nothing_without_a_valid_decision(
    action: Any, reviewer: Any, status: Any
) -> None:
    draft = {"status": "pass", "reasons": ["rule passed"], "confidence": 0.5}
    out, applied, events = apply_decision(
        draft, {"action": action, "reviewer": reviewer, "status": status}
    )
    if not _valid(action, reviewer, status):
        assert out["status"] == "needs_review" and events == ["no_decision"]
        assert applied["action"] == "none" and NO_DECISION in out["reasons"]
        return
    assert "no_decision" not in events
    if action == "approve":
        assert out["status"] == "pass" and events == ["human_approved"]
    elif action == "override":
        assert out["status"] == status and events == [f"human_override:{status}"]
    else:
        assert out == draft and events == ["human_regather"]


@pytest.mark.parametrize("received", [None, {}, "approve", ["approve"], 1, {"action": "approve"}])
def test_a_missing_or_malformed_decision_is_held_for_review(received: Any) -> None:
    out, applied, events = apply_decision({"status": "pass", "reasons": []}, received)
    assert out["status"] == "needs_review" and events == ["no_decision"]
    assert applied["action"] == "none"


@hsettings(max_examples=200, deadline=None)
@given(action=st.text(max_size=16))
def test_no_action_but_the_three_builds_a_decision(action: str) -> None:
    if action in decisions.ACTIONS:
        built = decisions.build(action, "auditor", status="fail" if action == "override" else None)
        assert built["action"] == action
    else:
        with pytest.raises(decisions.InvalidDecision):
            decisions.build(action, "auditor")


def test_build_refuses_ambiguous_decisions() -> None:
    for args in (
        ("override", "auditor", None, None),  # no status
        ("override", "auditor", None, "passed"),  # not a status
        ("approve", "auditor", None, "pass"),  # a status means an override
        ("regather", "auditor", None, "fail"),
        ("approve", "  ", None, None),  # no reviewer
    ):
        with pytest.raises(decisions.InvalidDecision):
            decisions.build(*args)
    assert decisions.validate(decisions.build("override", "a", "n", "fail"))["status"] == "fail"
    with pytest.raises(decisions.InvalidDecision):
        decisions.validate(None)


@hsettings(max_examples=200, deadline=None)
@given(
    decision=st.one_of(st.just("approve"), st.text(max_size=12)),
    status=st.one_of(st.none(), st.text(max_size=8)),
    kind=st.sampled_from(["remediation", "remediation_rollback", "policy_draft"]),
)
def test_plain_items_take_only_a_plain_approve(
    decision: str, status: str | None, kind: str
) -> None:
    if decision == "approve" and status is None:
        actions.check_plain_decision(kind, decision, status)
    else:
        with pytest.raises(actions.InvalidDecision):
            actions.check_plain_decision(kind, decision, status)


# -- against a scratch database ------------------------------------------------------------


def _providers() -> tier3.Providers:
    return tier3.Providers(
        embedder=HashingEmbeddingClient(),
        reranker=None,
        llm=StubLLMClient(handler=stub_extract_agent, chat_handler=stub_chat_search_once),
        retrieval=RetrievalConfig(),
    )


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, tier3.Providers]]:
    with prepared_tenants(base_settings, "ccp_fclosed", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = _providers()
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        # no critical systems list: HR-4 stops at the gate for a person every time
        acme.execute("DELETE FROM chunks WHERE evidence_type = 'critical_systems_list'")
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1",))
        evaluations.drain(acme, settings, now=NOW)
        yield settings, acme, providers


def _interrupt(world: tuple[Settings, ScopedDB, tier3.Providers]) -> tuple[str, store.Approval]:
    settings, acme, providers = world
    spec = knowledge.load().tests["HR-4"]
    ctx = load_context(acme, settings, spec, NOW, NOW)
    verdict = tier3.evaluate(acme, settings, ctx, spec, "tenant", providers)
    assert verdict.status is VerdictStatus.needs_review
    thread_id = str((verdict.request_json or {})["thread_id"])
    item = store.for_ref(acme, "tier3_interrupt", thread_id)
    assert item is not None and item.is_open
    return thread_id, item


def _unapplied(acme: ScopedDB) -> set[str]:
    return {i.approval_id for i in decisions.unapplied(acme)}


@pytest.mark.db
def test_a_decision_is_recorded_first_and_survives_a_failed_resume(
    world: tuple[Settings, ScopedDB, tier3.Providers], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, acme, providers = world
    thread_id, item = _interrupt(world)

    def worker_died(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("worker died mid-resume")

    monkeypatch.setattr(tier3, "resume", worker_died)
    outcome = actions.approve(
        acme,
        settings,
        item.approval_id,
        member(acme, "auditor"),
        "inventory pending",
        decision="override",
        status="partial",
        providers=providers,
    )
    assert not outcome.ok and "resume failed" in outcome.detail
    assert f"ccp approvals resume {item.approval_id}" in outcome.detail
    decided = store.get(acme, item.approval_id)
    assert decided is not None and decided.state == "approved" and decided.decision is not None
    assert decided.decision["action"] == "override" and decided.decision["status"] == "partial"
    assert store.history(acme, item.approval_id)[-1]["decision"] == decided.decision
    row = threads.get(acme, thread_id)
    assert row is not None and row.status == "interrupted"
    assert item.approval_id in _unapplied(acme)
    monkeypatch.undo()
    with pytest.raises(ValueError):  # decided: it cannot be decided again
        actions.approve(acme, settings, item.approval_id, member(acme, "someone else"))
    resumed = actions.resume(acme, settings, item.approval_id, providers=providers)
    assert resumed.ok and "thread resumed: partial" in resumed.detail
    row = threads.get(acme, thread_id)
    assert row is not None and row.status == "finished" and row.decision == decided.decision
    assert item.approval_id not in _unapplied(acme) and decisions.audit(acme) == []
    with pytest.raises(ValueError, match="verdict is stored"):  # applied once, never twice
        actions.resume(acme, settings, item.approval_id, providers=providers)


@pytest.mark.db
def test_a_failed_commit_after_the_resume_is_retried_from_the_final_state(
    world: tuple[Settings, ScopedDB, tier3.Providers], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, acme, providers = world
    thread_id, item = _interrupt(world)

    def database_went_away(db: ScopedDB, verdict: Any) -> bool:
        raise RuntimeError("database went away")

    monkeypatch.setattr(evaluations, "commit_verdict", database_went_away)
    outcome = actions.approve(
        acme, settings, item.approval_id, member(acme, "auditor"), providers=providers
    )
    assert not outcome.ok
    row = threads.get(acme, thread_id)
    assert row is not None and row.status == "finished" and row.verdict_id
    assert verdicts.load(acme, row.verdict_id) is None  # finished, but nothing stored
    assert item.approval_id in _unapplied(acme)
    monkeypatch.undo()
    again = actions.resume(acme, settings, item.approval_id, providers=providers)
    assert again.ok and "thread resumed: needs_review" in again.detail
    stored = verdicts.load(acme, row.verdict_id)
    assert stored is not None and "human_approved" in stored.guardrail_events
    assert item.approval_id not in _unapplied(acme)


@pytest.mark.db
def test_an_item_that_does_not_show_the_pending_interrupt_records_nothing(
    world: tuple[Settings, ScopedDB, tier3.Providers],
) -> None:
    settings, acme, providers = world
    thread_id, item = _interrupt(world)
    doctored = {**item.payload, "draft": {**item.payload["draft"], "status": "pass"}}
    forged = str(ULID())
    acme.execute(
        "INSERT INTO approvals (approval_id, tenant_id, kind, ref_id, payload, payload_sha256)"
        " VALUES (%s, %s, 'tier3_interrupt', %s, %s, %s)",
        (forged, "acme", thread_id, json.dumps(doctored), store.payload_hash(doctored)),
    )
    acme.execute(
        "INSERT INTO approval_events (event_id, approval_id, tenant_id, event, reviewer)"
        " VALUES (%s, %s, %s, 'opened', 'system')",
        (str(ULID()), forged, "acme"),
    )
    with pytest.raises(actions.PayloadChanged):
        actions.approve(acme, settings, forged, member(acme, "auditor"), providers=providers)
    shown = store.get(acme, forged)
    assert shown is not None and shown.state == "opened" and shown.decision is None
    row = threads.get(acme, thread_id)
    assert row is not None and row.status == "interrupted"
    # the genuine item still decides the thread
    outcome = actions.reject(
        acme, settings, item.approval_id, member(acme, "auditor"), "not enough evidence"
    )
    assert outcome.ok and "needs_review" in outcome.detail
    row = threads.get(acme, thread_id)
    assert row is not None and row.status == "finished" and row.decision is not None
    assert row.decision["action"] == "override" and row.decision["status"] == "needs_review"


@pytest.mark.db
def test_a_stored_decision_without_a_reviewer_is_held_for_review(
    world: tuple[Settings, ScopedDB, tier3.Providers],
) -> None:
    settings, acme, providers = world
    thread_id, item = _interrupt(world)
    # bypass decide(): the event names a reviewer, the decision itself does not
    store.record_event(
        acme, item.approval_id, "approved", member(acme, "auditor"), decision={"action": "approve"}
    )
    final = tier3.resume(acme, settings, thread_id, providers, approval_id=item.approval_id)
    assert final.status is VerdictStatus.needs_review
    assert "no_decision" in final.guardrail_events and NO_DECISION in final.reasoning
    assert "human_approved" not in final.guardrail_events


@pytest.mark.db
def test_resume_needs_this_threads_recorded_decision(
    world: tuple[Settings, ScopedDB, tier3.Providers],
) -> None:
    settings, acme, providers = world
    thread_id, item = _interrupt(world)
    with pytest.raises(LookupError):  # nothing recorded: the graph does not move
        tier3.resume(acme, settings, thread_id, providers, approval_id=item.approval_id)
    other_thread, other = _interrupt(world)
    store.record_event(
        acme,
        other.approval_id,
        "approved",
        member(acme, "auditor"),
        decision=decisions.build("override", "auditor", status="pass"),
    )
    with pytest.raises(LookupError, match="not an item of thread"):  # another thread's decision
        tier3.resume(acme, settings, thread_id, providers, approval_id=other.approval_id)
    row = threads.get(acme, thread_id)
    assert row is not None and row.status == "interrupted"
    assert other_thread != thread_id


@pytest.mark.db
def test_a_rollback_needs_its_own_approval_bound_to_what_it_restores(
    world: tuple[Settings, ScopedDB, tier3.Providers],
) -> None:
    settings, acme, _ = world
    verdict = {v.subject: v for v in latest_per_subject(acme, "HR-1")}[JANE]

    def proposal() -> proposals.Proposal:
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
        item = store.for_ref(acme, "remediation", created.proposal_id)
        assert item is not None
        actions.approve(acme, settings, item.approval_id, member(acme, "security-lead"))
        return created

    # simulated: the rollback approval is the only gate
    simulated = proposal()
    assert runner.simulate(acme, settings, simulated.proposal_id).mode == "simulated"
    refused = runner.rollback(acme, settings, simulated.proposal_id)
    assert refused.mode == "refused" and "gate5:approval" in refused.detail
    undo = store.for_ref(acme, "remediation_rollback", simulated.proposal_id)
    assert undo is not None and undo.is_open
    with pytest.raises(actions.InvalidDecision):  # not a tier 3 item: no decisions
        actions.approve(acme, settings, undo.approval_id, member(acme, "cto"), decision="override")
    actions.approve(acme, settings, undo.approval_id, member(acme, "cto"), "undo the drill")
    rolled = runner.rollback(acme, settings, simulated.proposal_id)
    assert rolled.mode == "rolled_back" and rolled.proposal.status == "rolled_back"

    # live: approved, then the restored permission is edited: the approval no longer matches
    live_settings = settings.model_copy(update={"ccp_remediation_live": True})
    executed = proposal()
    github = RecordingGitHubAdmin(permissions={("acme-example", "platform", "jsmith"): "write"})
    done = runner.execute(
        acme,
        live_settings,
        executed.proposal_id,
        live=True,
        confirm=lambda p: "remove jsmith from acme-example/platform",
        github=github,
        config=ALLOWED,
    )
    assert done.mode == "executed"
    first = runner.rollback(acme, live_settings, executed.proposal_id, github=github)
    assert first.mode == "refused"
    undo = store.for_ref(acme, "remediation_rollback", executed.proposal_id)
    assert undo is not None and undo.payload["restore_permission"] == "write"
    actions.approve(acme, settings, undo.approval_id, member(acme, "cto"))
    acme.execute(
        "UPDATE remediation_proposals SET pre_state = %s WHERE proposal_id = %s",
        ('{"permission": "admin"}', executed.proposal_id),
    )
    restore = "restore jsmith to acme-example/platform"
    escalated = runner.rollback(
        acme,
        live_settings,
        executed.proposal_id,
        live=True,
        confirm=lambda p: restore,
        github=github,
        config=ALLOWED,
    )
    assert escalated.mode == "refused" and "gate5:approval" in escalated.detail
    assert all(c["method"] != "PUT" for c in github.calls)
    # with the recorded permission back, the same approval holds again
    acme.execute(
        "UPDATE remediation_proposals SET pre_state = %s WHERE proposal_id = %s",
        ('{"permission": "write"}', executed.proposal_id),
    )
    for missing in ("gate1", "gate2", "gate3", "gate4"):
        result = runner.rollback(
            acme,
            live_settings if missing != "gate1" else settings,
            executed.proposal_id,
            live=missing != "gate2",
            confirm=lambda p, m=missing: "no" if m == "gate4" else restore,
            github=github,
            config=ALLOWED if missing != "gate3" else gates.RemediationConfig(),
        )
        assert result.mode in ("refused", "dry_run"), missing
        assert all(c["method"] != "PUT" for c in github.calls)
    rolled = runner.rollback(
        acme,
        live_settings,
        executed.proposal_id,
        live=True,
        confirm=lambda p: restore,
        github=github,
        config=ALLOWED,
    )
    assert rolled.mode == "rolled_back" and github.calls[-1]["permission"] == "write"
    # the request opened for the edited payload was never approved, and is closed now
    requests = store.items_for_ref(acme, "remediation_rollback", executed.proposal_id)
    assert [i.state for i in requests] == ["approved", "closed"]
    assert requests[1].payload["restore_permission"] == "admin"
    assert runner.audit(acme) == []


@pytest.mark.db
def test_the_audits_find_actions_without_approvals(
    world: tuple[Settings, ScopedDB, tier3.Providers],
) -> None:
    settings, acme, providers = world
    verdict = {v.subject: v for v in latest_per_subject(acme, "HR-1")}[JANE]
    rogue = proposals.create(
        acme,
        verdict_id=verdict.verdict_id,
        source="rule",
        action="remove_repo_collaborator",
        target={"repo": "acme-example/platform", "login": "jsmith"},
        rationale="test",
        reversible=True,
        live_capable=True,
    )
    proposals.update(acme, rogue.proposal_id, status="executed")  # acted, never approved
    assert any(rogue.proposal_id in p for p in runner.audit(acme))
    thread_id, item = _interrupt(world)
    store.record_event(
        acme,
        item.approval_id,
        "approved",
        member(acme, "auditor"),
        decision=decisions.build("approve", "auditor"),
    )
    outcome = actions.resume(acme, settings, item.approval_id, providers=providers)
    assert outcome.ok
    assert decisions.audit(acme) == []
    acme.execute(
        "UPDATE agent_threads SET decision = %s WHERE thread_id = %s",
        (json.dumps({"action": "override", "status": "pass", "reviewer": "nobody"}), thread_id),
    )
    assert any(thread_id in p for p in decisions.audit(acme))
