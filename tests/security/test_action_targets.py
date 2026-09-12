"""S19 (targets half): a proposal must name this tenant's entities and the grant the verdict
cited; anything else is dropped with a guardrail event."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators.verdict import Verdict, latest_per_subject
from ccp.remediation import actions, runner, targets
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
JANE = "person:hris:person:E1042"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB], Verdict]]:
    with prepared_tenants(base_settings, "ccp_targets") as (settings, dbs):
        acme = dbs["acme"]
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1", "HR-5"))
        evaluations.drain(acme, settings, now=NOW)
        jane = {v.subject: v for v in latest_per_subject(acme, "HR-1")}[JANE]
        yield settings, dbs, jane


def test_rule_fallback_proposes_the_tabled_actions_for_jane(world) -> None:  # type: ignore[no-untyped-def]
    _, dbs, jane = world
    drafts = actions.rule_fallback(dbs["acme"], jane)
    assert [(d.action, d.target) for d in drafts] == [
        ("remove_repo_collaborator", {"repo": "acme-example/platform", "login": "jsmith"}),
        ("remove_org_member", {"org": "acme-example", "login": "jsmith"}),
        ("notify_owner", {"subject_key": "HR-1|person:hris:person:E1042"}),
    ]
    summary = runner.propose(dbs["acme"], jane)
    assert [p.action for p in summary.proposals] == [
        "remove_repo_collaborator",
        "remove_org_member",
        "notify_owner",
    ]
    assert summary.rejected == []
    assert [p.live_capable for p in summary.proposals] == [True, False, False]
    again = runner.propose(dbs["acme"], jane)  # idempotent per verdict
    assert [p.proposal_id for p in again.proposals] == [p.proposal_id for p in summary.proposals]


def test_another_tenants_repo_is_rejected_with_a_guardrail_event(world) -> None:  # type: ignore[no-untyped-def]
    _, dbs, jane = world
    globex_repo = targets.resolve(
        dbs["acme"],
        jane,
        "remove_repo_collaborator",
        {"repo": "globex-example/platform", "login": "jsmith"},
    )
    assert not globex_repo.ok and globex_repo.events == (
        "target_rejected:unknown_repo:globex-example/platform",
    )
    other_org = targets.resolve(
        dbs["acme"], jane, "remove_org_member", {"org": "globex-example", "login": "jsmith"}
    )
    assert not other_org.ok and other_org.events[0].startswith("target_rejected:unknown_org")


def test_a_login_the_verdict_never_cited_is_rejected(world) -> None:  # type: ignore[no-untyped-def]
    _, dbs, jane = world
    uncited = targets.resolve(
        dbs["acme"],
        jane,
        "remove_repo_collaborator",
        {"repo": "acme-example/platform", "login": "dokafor-dev"},
    )
    assert not uncited.ok and uncited.events == (
        "target_rejected:uncited_grant:remove_repo_collaborator:dokafor-dev",
    )
    unknown = targets.resolve(
        dbs["acme"],
        jane,
        "remove_repo_collaborator",
        {"repo": "acme-example/platform", "login": "nobody"},
    )
    assert not unknown.ok and unknown.events[0].startswith("target_rejected:unknown_login")
    ok = targets.resolve(
        dbs["acme"],
        jane,
        "remove_repo_collaborator",
        {"repo": "acme-example/platform", "login": "jsmith"},
    )
    assert ok.ok and ok.grant_entity_ids == ("github:grant:U_kgDOAcme1042:R_kgDOAcmeP001",)


def test_hr5_fail_proposes_only_a_notification(world) -> None:  # type: ignore[no-untyped-def]
    _, dbs, _ = world
    hr5 = {v.subject: v for v in latest_per_subject(dbs["acme"], "HR-5")}["tenant"]
    drafts = actions.rule_fallback(dbs["acme"], hr5)
    assert [d.action for d in drafts] == ["notify_owner"] and "ops-shared" in drafts[0].rationale
