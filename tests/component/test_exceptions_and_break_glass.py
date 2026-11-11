"""MT4 (A-132): exceptions and break-glass go through the approval service.

The defects: nothing let a person accept a known failure for a while and answer for it, so
an accepted risk was either invisible or looked like a pass; and when the only approver at
hand was the person asking, there was no recorded way to act, so the rule was either
broken silently or the action waited.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from ccp.approvals import actions, grants
from ccp.approvals import store as approvals
from ccp.auth import members
from ccp.compliance_state import exceptions, reader, writer
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators.verdict import latest_per_subject
from ccp.knowledge import rollup as knowledge_rollup
from ccp.remediation import proposals
from ccp.storage import exceptions as stored
from ccp.storage.scoped import ScopedDB
from tests.support.members import member
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
JANE = "person:hris:person:E1042"
REASON = "contractor keeps read access until the data migration ends"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_except", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1",))
        evaluations.drain(acme, settings, now=NOW)
        writer.drain(acme)
        yield settings, acme


def _until(days: int = 30) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def test_an_exception_shows_as_an_excepted_failure_never_a_pass(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    asker = member(acme, "rita", "reviewer")
    until = _until()
    item = exceptions.request(
        acme, asker, test_id="HR-1", subject=JANE, until=until, reason=REASON, owner="cto"
    )
    pending = approvals.get(acme, item)
    assert pending is not None and pending.kind == "exception" and pending.is_open
    assert pending.payload["shows_as"] == f"fail (excepted until {until:%Y-%m-%d})"
    before = reader.framework_rollup(acme, "soc2")
    with pytest.raises(members.NotPermitted):  # a reviewer may ask, not decide
        actions.approve(acme, settings, item, asker)
    done = actions.approve(acme, settings, item, member(acme, "ana"))
    assert "never as pass" in done.detail
    held = stored.active(acme)[f"HR-1|{JANE}"]
    assert held.owner == "cto" and held.granted_by.endswith("#ana") and held.reason == REASON
    after = reader.framework_rollup(acme, "soc2")
    assert after.summary == before.summary  # the failure still counts as a failure
    shown = knowledge_rollup.format_rollup(after)
    blocked = [r for r in after.requirements if r.blocking_subject_key == f"HR-1|{JANE}"]
    assert blocked and all(r.state == "fail" for r in blocked)
    if all(r.excepted_until is not None for r in blocked):
        assert f"fail (excepted until {until:%Y-%m-%d})" in shown
    row = reader.current_state(acme, f"HR-1|{JANE}")
    assert row is not None and row.status.value == "fail"
    assert "excepted until" in reader.format_row(
        row, stored.excepted_until(acme).get(row.subject_key)
    )
    # it lapses on its date: after that, a plain failure again
    assert f"HR-1|{JANE}" not in stored.active(acme, now=until + timedelta(seconds=1))
    # and an approver may end it early
    exceptions.revoke(acme, member(acme, "ana"), held.exception_id, "the migration finished")
    assert f"HR-1|{JANE}" not in stored.active(acme)
    with pytest.raises(exceptions.NotExceptable):
        exceptions.revoke(acme, member(acme, "ana"), held.exception_id, "again")


def test_only_a_failing_subject_takes_a_bounded_accountable_exception(
    world: tuple[Settings, ScopedDB],
) -> None:
    _, acme = world
    asker = member(acme, "rita", "reviewer")
    cases = [
        ({"subject": "person:hris:person:E0000"}, "only a failing subject"),  # never assessed
        ({"until": datetime.now(UTC) - timedelta(days=1)}, "ends in the future"),
        ({"until": _until(400)}, "at most 365 days"),
        ({"reason": "because"}, "at least 20 characters"),
        ({"owner": " "}, "name the owner"),
    ]
    for change, message in cases:
        asked = {"subject": JANE, "until": _until(), "reason": REASON, "owner": "cto"} | change
        with pytest.raises(exceptions.NotExceptable, match=message):
            exceptions.request(acme, asker, test_id="HR-1", **asked)  # type: ignore[arg-type]
    viewer = member(acme, "vic", "viewer")
    with pytest.raises(members.NotPermitted):
        exceptions.request(
            acme, viewer, test_id="HR-1", subject=JANE, until=_until(), reason=REASON, owner="x"
        )


def _remediation(acme: ScopedDB, asker: str) -> str:
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
        requested_by=asker,
    )
    item = approvals.for_ref(acme, "remediation", created.proposal_id)
    assert item is not None
    return item.approval_id


def test_break_glass_acts_now_and_another_approver_reviews_it(
    world: tuple[Settings, ScopedDB], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, acme = world
    admin = member(acme, "olu", "admin")
    item = _remediation(acme, admin.principal_id)
    with pytest.raises(members.NotPermitted, match="asked for this"):  # the usual rule holds
        actions.approve(acme, settings, item, admin)
    with pytest.raises(ValueError, match="at least 20"):
        actions.break_glass(acme, settings, item, admin, "urgent")
    with pytest.raises(members.NotPermitted, match="needs admin"):
        actions.break_glass(acme, settings, item, member(acme, "ana"), REASON)
    done = actions.break_glass(acme, settings, item, admin, "terminated engineer still pushes code")
    assert done.approval.state == "approved" and done.approval.break_glass
    assert grants.for_approval(acme, item) is not None  # it may act, once, like any approval
    review = approvals.for_ref(acme, "break_glass_review", item)
    assert review is not None and review.is_open and review.requested_by == admin.principal_id
    assert review.payload["reason"] == "terminated engineer still pushes code"
    with pytest.raises(members.NotPermitted, match="asked for this"):  # not their own review
        actions.approve(acme, settings, review.approval_id, admin)
    assert [p for p in approvals.break_glass_problems(acme) if item in p] == []  # due, not late
    actions.approve(acme, settings, review.approval_id, member(acme, "ana"), "justified")
    later = datetime.now(UTC) + approvals.EXPIRES["break_glass_review"] + timedelta(days=1)
    assert [p for p in approvals.break_glass_problems(acme, now=later) if item in p] == []


def test_break_glass_is_for_urgent_kinds_and_rationed(world: tuple[Settings, ScopedDB]) -> None:
    settings, acme = world
    admin = member(acme, "kai", "admin")
    draft = approvals.open_item(acme, "policy_draft", "draft-bg", {"draft": "x"})
    with pytest.raises(members.NotPermitted, match="not urgent"):
        actions.break_glass(acme, settings, draft, admin, REASON)
    used = acme.fetch_one(
        "SELECT count(*) FROM approval_events WHERE break_glass"
        " AND recorded_at > now() - interval '1 day'"
    )
    assert used is not None
    for _ in range(approvals.BREAK_GLASS_PER_DAY - int(used[0])):
        actions.break_glass(acme, settings, _remediation(acme, admin.principal_id), admin, REASON)
    with pytest.raises(members.NotPermitted, match="the limit"):
        actions.break_glass(acme, settings, _remediation(acme, admin.principal_id), admin, REASON)
    expired = approvals.open_item(
        acme, "remediation_rollback", "rb-expired", {"x": 1}, expires_in=timedelta(seconds=-1)
    )
    with pytest.raises((approvals.Expired, members.NotPermitted)):
        actions.break_glass(acme, settings, expired, admin, REASON)
    # an unreviewed use is a finding once its review is overdue
    later = datetime.now(UTC) + approvals.EXPIRES["break_glass_review"] + timedelta(days=1)
    assert approvals.break_glass_problems(acme, now=later)
