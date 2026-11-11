"""MT4 (A-130): a decision is made by a signed-in member with the role it needs, never by its
requester, never after it expires, and once.

The defects: a reviewer was whatever name was typed; nothing made a decision unique, so two
reviewers approving at once both recorded one; a request could be approved by whoever made
it; and an item waited for a decision forever.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from ccp.approvals import actions, store
from ccp.auth import members
from ccp.auth.dev_issuer import DEV_ISSUER
from ccp.auth.tokens import Principal
from ccp.config import Settings
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from tests.support.members import member

pytestmark = pytest.mark.db


@pytest.fixture
def own(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with scratch_database(base_settings, prefix="ccp_decide") as settings:
            pool = app_pool(settings, max_size=4)
            try:
                yield settings, ScopedDB(pool, TenantContext.for_id(settings, "acme"))
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


def _draft(db: ScopedDB, ref: str = "draft-1", **opened: Any) -> str:
    return store.open_item(db, "policy_draft", ref, {"draft": ref, "text": "policy"}, **opened)


def test_a_typed_name_cannot_decide(own: tuple[Settings, ScopedDB]) -> None:
    _, acme = own
    item = _draft(acme)
    with pytest.raises(members.NotPermitted, match="signed-in member"):
        store.record_event(acme, item, "approved", "cto")
    assert store.get(acme, item).state == "opened"  # type: ignore[union-attr]


def test_a_decision_needs_the_role_for_it(own: tuple[Settings, ScopedDB]) -> None:
    settings, acme = own
    item = _draft(acme)
    reviewer = member(acme, "rita", "reviewer")
    with pytest.raises(members.NotPermitted, match="this needs approver"):
        actions.approve(acme, settings, item, reviewer)
    actions.request_changes(acme, item, reviewer, "say who owns it")  # a reviewer may ask
    stranger = Principal(DEV_ISSUER, "sam")  # signed in, but not a member here
    with pytest.raises(members.NotPermitted, match="not a member"):
        actions.approve(acme, settings, item, stranger)
    done = actions.approve(acme, settings, item, member(acme, "ana"))
    assert (done.approval.state, done.approval.reviewer) == ("approved", "ana")
    assert done.approval.principal_id == f"{DEV_ISSUER}#ana"


def test_whoever_asked_may_not_decide(own: tuple[Settings, ScopedDB]) -> None:
    settings, acme = own
    asker = member(acme, "olu")
    item = _draft(acme, requested_by=asker.principal_id)
    with pytest.raises(members.NotPermitted, match="asked for this"):
        actions.approve(acme, settings, item, asker)
    assert actions.approve(acme, settings, item, member(acme, "ana")).approval.state == "approved"


def test_an_expired_item_takes_no_decision(own: tuple[Settings, ScopedDB]) -> None:
    settings, acme = own
    item = _draft(acme, expires_in=timedelta(seconds=-1))
    with pytest.raises(store.Expired, match="expired"):
        actions.approve(acme, settings, item, member(acme, "ana"))
    again = _draft(acme)  # asking again opens a fresh item; the expired one stays as it was
    assert again != item and store.get(acme, item).state == "opened"  # type: ignore[union-attr]


def test_the_payload_shown_is_the_payload_decided(own: tuple[Settings, ScopedDB]) -> None:
    settings, acme = own
    item = _draft(acme)
    with pytest.raises(actions.PayloadChanged, match="nothing was recorded"):
        actions.approve(acme, settings, item, member(acme, "ana"), seen_sha256="0" * 64)
    shown = store.get(acme, item)
    assert shown is not None and shown.state == "opened"
    done = actions.approve(
        acme, settings, item, member(acme, "ana"), seen_sha256=shown.payload_sha256
    )
    assert done.approval.state == "approved"


def test_two_approvers_at_once_make_one_decision(
    own: tuple[Settings, ScopedDB], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, acme = own
    item = _draft(acme)
    ana, bo = member(acme, "ana"), member(acme, "bo")
    both_checked = threading.Barrier(2, timeout=10)
    real = store._authorize

    def race(*args: Any) -> Any:  # both have read the item as open before either writes
        found = real(*args)
        both_checked.wait()
        return found

    monkeypatch.setattr(store, "_authorize", race)
    outcomes: list[str] = []

    def decide(who: Principal) -> None:
        try:
            store.record_event(acme, item, "approved", who, "mine")
            outcomes.append("approved")
        except store.AlreadyDecided:
            outcomes.append("refused")

    runs = [threading.Thread(target=decide, args=(p,)) for p in (ana, bo)]
    for run in runs:
        run.start()
    for run in runs:
        run.join(timeout=20)
    assert sorted(outcomes) == ["approved", "refused"]
    decided = acme.fetch_all(
        "SELECT event FROM approval_events WHERE approval_id = %s AND event = 'approved'",
        (item,),
    )
    assert len(decided) == 1
