"""MT8 (A-123): no PASS over a partial or stale population.

The defect: a PASS that rests on an absence (no admin over the limit, no access left after a
termination) was recorded whatever the pull behind it had seen. A member listing the
credential could not see read as a population with no admins over the limit, and a
population last seen whole a month ago passed as if seen today.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from ccp.config import Settings
from ccp.connectors.github.client import GitHubLiveConnector
from ccp.evaluators import runner
from ccp.evaluators.verdict import Verdict, latest_per_subject
from ccp.ingest.landing import land_events, sync_connector
from ccp.ingest.raw_zone import RawZone
from ccp.knowledge import graph as knowledge
from ccp.normalize.consumer import normalize_all
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext, TenantId
from tests.component.test_github_recorded import ORG, _api

pytestmark = pytest.mark.db

ORG_SUBJECT = f"org:github:asset:org:{ORG}"


@pytest.fixture
def own(base_settings: Settings, tmp_path: Path) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with scratch_database(base_settings, prefix="ccp_req") as settings:
            local = settings.model_copy(update={"ccp_data_dir": tmp_path})
            pool = app_pool(local, max_size=2)
            try:
                yield local, ScopedDB(pool, TenantContext.for_id(local, "acme"))
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


def _connector(**api: Any) -> GitHubLiveConnector:
    handler, _ = _api(["ana"], **api)
    return GitHubLiveConnector("t", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def _pull(settings: Settings, db: ScopedDB, **api: Any) -> str:
    """One recorded pull, landed as a sync run and normalized; returns the run's status."""
    run = sync_connector(settings, db, db.tenant, _connector(**api))
    normalize_all(db, settings)
    return run.status


def _gh2(settings: Settings, db: ScopedDB, now: datetime | None = None) -> Verdict:
    runner.enqueue_all(db, settings, now, tests=("GH-2",), tiers=(1,))
    runner.drain(db, settings, now)
    return {v.subject: v for v in latest_per_subject(db, "GH-2")}[ORG_SUBJECT]


def _with_limit(db: ScopedDB, limit: int) -> ScopedDB:
    settings = db.tenant.settings.model_copy(update={"org_admin_limit": limit})
    return ScopedDB(db._pool, TenantContext(TenantId(db.tenant.tenant_id), settings))


def test_a_hidden_member_listing_withholds_the_pass(own: tuple[Settings, ScopedDB]) -> None:
    settings, acme = own
    assert _pull(settings, acme) == "complete"
    assert _gh2(settings, acme).status.value == "pass"  # one admin, the limit is three
    hidden = {f"/orgs/{ORG}/members": [httpx.Response(404)] * 2}
    assert _pull(settings, acme, overrides=hidden) == "partial"
    withheld = _gh2(settings, acme)
    assert withheld.status.value == "needs_review"
    assert "incomplete_source:github:members" in withheld.guardrail_events
    assert "withheld pass" in withheld.reasoning
    assert "github:members was not visible in its latest pull" in withheld.reasoning
    assert _pull(settings, acme) == "complete"  # seen whole again: the pass returns
    assert _gh2(settings, acme).status.value == "pass"


def test_a_population_last_seen_whole_too_long_ago_withholds_the_pass(
    own: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = own
    _pull(settings, acme)
    assert _gh2(settings, acme).status.value == "pass"
    later = datetime.now(UTC) + timedelta(hours=settings.ccp_sync_stale_hours + 1)
    stale = _gh2(settings, acme, later)  # no new entity, but the key moved with the sources
    assert stale.status.value == "needs_review"
    assert "github:members was last seen whole at" in stale.reasoning


def test_a_fail_stands_over_a_partial_population(own: tuple[Settings, ScopedDB]) -> None:
    settings, acme = own
    strict = _with_limit(acme, 0)  # one admin over a limit of none
    _pull(settings, strict)
    assert _gh2(settings, strict).status.value == "fail"
    _pull(settings, strict, overrides={f"/orgs/{ORG}/members": [httpx.Response(404)] * 2})
    assert _gh2(settings, strict).status.value == "fail"  # a violation seen is a violation


def test_data_with_no_recorded_pull_cannot_pass(own: tuple[Settings, ScopedDB]) -> None:
    settings, acme = own
    events = list(_connector().pull(acme.tenant))  # landed by hand: no sync run behind it
    land_events(acme, RawZone(settings.raw_dir), events)
    normalize_all(acme, settings)
    verdict = _gh2(settings, acme)
    assert verdict.status.value == "needs_review"
    assert "github:members has no recorded pull" in verdict.reasoning


def test_a_test_may_require_only_a_scope_some_connector_declares() -> None:
    fields: dict[str, Any] = {
        "test_id": "XX-1",
        "title": "x",
        "connector_scope": ["github"],
        "evaluation": "deterministic",
        "evidence_types": ["org_membership"],
        "subject_kind": "org",
        "controls": ["CTL-PRIV-LIMIT"],
        "rule": "x",
        "valid_from_strategy": "evidence_observed",
    }
    assert knowledge.TestSpec.model_validate({**fields, "requires_complete": ["github:members"]})
    with pytest.raises(ValueError, match="unknown source scope"):
        knowledge.TestSpec.model_validate({**fields, "requires_complete": ["github:membership"]})
    with pytest.raises(ValueError, match="unknown source scope"):
        knowledge.TestSpec.model_validate({**fields, "requires_complete": ["okta:users"]})
