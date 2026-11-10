"""MT1 (A-119): a listing seen whole settles every grant on it at the pull time.

The defects: snapshot ids come from content (A-102), so a pull that sees an unchanged member
lands nothing, and the grant stays last observed at the pull that first saw it. A person
terminated after that pull who still holds the access long past the deadline could only ever
be needs_review on HR-1 and GH-1: nothing said the access was still there. And a collaborator
removed and then added back produces the same repository snapshot as before, which never
lands again, so the grant stayed revoked while the access was back.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from ccp.config import Settings
from ccp.connectors.base import SourceEvent
from ccp.connectors.github.client import GitHubLiveConnector
from ccp.evaluators import runner
from ccp.evaluators.verdict import latest_per_subject
from ccp.identity import resolver
from ccp.ingest.landing import land_events
from ccp.ingest.raw_zone import RawZone
from ccp.normalize.consumer import normalize_all
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from tests.component.test_github_recorded import _api

pytestmark = pytest.mark.db

ANA_ORG = "github:grant:U_ANA:org:acme-example"
BO_ORG = "github:grant:U_BO:org:acme-example"
BO_REPO = "github:grant:U_BO:R_1"


@pytest.fixture
def lone(base_settings: Settings, tmp_path: Path) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with scratch_database(base_settings, prefix="ccp_presence") as settings:
            local = settings.model_copy(update={"ccp_data_dir": tmp_path})
            pool = app_pool(local, max_size=2)
            try:
                yield local, ScopedDB(pool, TenantContext.for_id(local, "acme"))
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


def _on(day: int) -> datetime:
    return datetime(2026, 9, day, tzinfo=UTC)  # the recorded repo was last updated 1 Sep


def _pull(settings: Settings, db: ScopedDB, collaborators: list[str], day: int, **api: Any) -> int:
    """One live pull on the given day against the recorded API, landed and normalized."""
    handler, _ = _api(collaborators, emails={"ana": ["ana@acme.example"]}, **api)
    connector = GitHubLiveConnector(
        "t", transport=httpx.MockTransport(handler), sleep=lambda s: None, clock=lambda: _on(day)
    )
    landed, _ = land_events(db, RawZone(settings.raw_dir), connector.pull(db.tenant))
    normalize_all(db, settings)
    return landed


def _state(db: ScopedDB, grant: str) -> tuple[str, datetime]:
    row = db.fetch_one(
        "SELECT state, observed_at FROM current_access_grants WHERE entity_id = %s", (grant,)
    )
    assert row is not None
    return str(row[0]), row[1]


def _terminate_ana(settings: Settings, db: ScopedDB) -> None:
    roster = SourceEvent(
        tenant_id=db.tenant.tenant_id,
        connector="hris",
        source_event_id="E7-roster",
        source_ts=_on(6),
        event_type="hris.employee.snapshot",
        payload={
            "employee_id": "E7",
            "display_name": "Ana Lima",
            "work_email": "ana@acme.example",
            "status": "terminated",
            "hired_at": "2025-01-06",
            "terminated_at": "2026-09-06T00:00:00Z",
            "title": "Engineer",
            "department": "Platform",
            "employment_type": "full_time",
        },
        schema_version="hris.v1",
    )
    land_events(db, RawZone(settings.raw_dir), [roster])
    normalize_all(db, settings)


def _verdicts(settings: Settings, db: ScopedDB) -> dict[str, dict[str, str]]:
    resolver.resolve_tenant(db)
    runner.enqueue_all(db, settings, tests=("HR-1", "GH-1"), tiers=(1,))
    runner.drain(db, settings)
    return {
        test: {v.subject: v.status.value for v in latest_per_subject(db, test)}
        for test in ("HR-1", "GH-1")
    }


def test_access_still_held_past_the_deadline_fails_instead_of_needs_review(
    lone: tuple[Settings, ScopedDB],
) -> None:
    settings, db = lone
    _pull(settings, db, ["ana"], day=5)  # ana: a member, and a collaborator on the repo
    _terminate_ana(settings, db)  # terminated 6 Sep; with the 24 h SLA the deadline is 7 Sep
    before = _verdicts(settings, db)
    (subject,) = before["HR-1"]
    assert before == {"HR-1": {subject: "needs_review"}, "GH-1": {subject: "needs_review"}}
    assert _pull(settings, db, ["ana"], day=10) == 1  # nothing changed: only the marker lands
    assert _state(db, ANA_ORG) == ("active", _on(10))  # still there on the 10th
    row = db.fetch_one(
        "SELECT attributes FROM current_access_grants WHERE entity_id = %s", (ANA_ORG,)
    )
    assert row is not None and row[0]["confirmed_by"] == "complete_pull"
    after = _verdicts(settings, db)
    assert after == {"HR-1": {subject: "fail"}, "GH-1": {subject: "fail"}}


def test_a_collaborator_added_back_is_active_again(lone: tuple[Settings, ScopedDB]) -> None:
    settings, db = lone
    _pull(settings, db, ["ana", "bo"], day=5)
    _pull(settings, db, ["ana"], day=6)  # bo removed from the repository
    assert _state(db, BO_REPO) == ("revoked", _on(6))
    assert _pull(settings, db, ["ana", "bo"], day=8) == 1  # the 5 Sep repo snapshot again
    assert _state(db, BO_REPO) == ("active", _on(8))
    history = db.fetch_all(
        "SELECT state, role FROM access_grants WHERE entity_id = %s ORDER BY observed_at",
        (BO_REPO,),
    )
    # the 1 Sep snapshot, seen on the 5th, revoked on the 6th, back on the 8th
    assert [r[0] for r in history] == ["active", "active", "revoked", "active"]
    assert history[-1][1] == "write"  # the role it last had while active


def test_a_listing_not_seen_whole_settles_nothing_on_it(lone: tuple[Settings, ScopedDB]) -> None:
    settings, db = lone
    _pull(settings, db, ["ana", "bo"], day=5)
    _pull(settings, db, ["ana"], day=6, collaborators_status=404)  # collaborators not visible
    assert _state(db, BO_REPO) == ("active", _on(5))  # neither revoked nor confirmed
    assert _state(db, BO_ORG) == ("active", _on(6))  # the members listing was seen whole
