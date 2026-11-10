"""MT1 (A-118): every pull is a sync run, recorded scope by scope.

The defects: `pull()` was the whole contract, so nothing recorded whether a pull saw
everything; ingest ran the connectors one after another with no isolation, so an exception
in one stopped the ones after it and left no record but a log line; and a pull that could not
see a listing looked the same, in the database, as one that saw an empty one.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest

from ccp.config import Settings
from ccp.connectors.base import ConnectorName, SourceEvent, SourceKind, SyncReport
from ccp.connectors.github.client import GitHubLiveConnector
from ccp.connectors.registry import build_connector
from ccp.ingest import landing, sync_runs
from ccp.ingest.landing import ingest_tenant, sync_connector
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from tests.component.test_github_recorded import _api

pytestmark = pytest.mark.db


@pytest.fixture
def own(base_settings: Settings, tmp_path: Any) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with scratch_database(base_settings, prefix="ccp_sync") as settings:
            run_settings = settings.model_copy(update={"ccp_data_dir": tmp_path})
            pool = app_pool(settings, max_size=2)
            try:
                yield run_settings, ScopedDB(pool, TenantContext.for_id(settings, "acme"))
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


class _Breaks:
    """Lands two events, marks its first scope, then fails mid-pull."""

    name: ConnectorName = "hris"
    source: SourceKind = "fixture"
    scopes: tuple[str, ...] = ("employees", "documents")

    def pull(
        self, tenant: TenantContext, since: datetime | None = None, report: SyncReport | None = None
    ) -> Iterator[SourceEvent]:
        for n in (1, 2):
            yield SourceEvent(
                tenant_id=tenant.tenant_id,
                connector="hris",
                source_event_id=f"break-{n}",
                source_ts=datetime(2026, 9, 1, tzinfo=UTC),
                event_type="hris.employee.snapshot",
                payload={"employee_id": f"E{n}"},
                schema_version="hris.v1",
            )
        if report is not None:
            report.complete("employees", 2)
        raise ConnectionError("the provider hung up")


def test_a_fixture_ingest_records_one_complete_run_per_connector(
    own: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = own
    summary = ingest_tenant(settings, acme, acme.tenant)
    assert [(r.connector, r.status) for r in summary.runs] == [
        ("github", "complete"),
        ("hris", "complete"),
        ("manual", "complete"),
    ]
    for run in summary.runs:
        connector = build_connector(run.connector, settings, "fixture")  # type: ignore[arg-type]
        assert set(run.scopes) == set(connector.scopes)
        assert all(o.status == "complete" for o in run.scopes.values())
    recorded = sync_runs.recent(acme)
    assert {r["connector"] for r in recorded} == {"github", "hris", "manual"}
    assert sync_runs.last_complete(acme, "github", "members") is not None


def test_one_connector_failing_ends_only_its_own_run(
    own: tuple[Settings, ScopedDB], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, acme = own
    real = landing.build_connector

    def build(name: Any, s: Settings, source: Any = "fixture") -> Any:
        return _Breaks() if name == "github" else real(name, s, source)

    monkeypatch.setattr(landing, "build_connector", build)
    summary = ingest_tenant(settings, acme, acme.tenant)
    broken, hris, manual = summary.runs
    assert broken.status == "failed" and broken.error_class == "ConnectionError"
    assert broken.landed == 2  # what landed before the failure is kept, and counted
    assert broken.scopes["employees"].status == "complete"
    assert broken.scopes["documents"].status == "failed"  # never reached
    assert (hris.status, manual.status) == ("complete", "complete")  # the next ones still ran
    assert hris.landed > 0


def test_a_live_pull_that_cannot_see_a_listing_is_a_partial_run(
    own: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = own
    handler, _ = _api(["ana"], collaborators_status=404)
    connector = GitHubLiveConnector(
        "t", transport=httpx.MockTransport(handler), sleep=lambda s: None
    )
    run = sync_connector(settings, acme, acme.tenant, connector)
    assert run.status == "partial"
    statuses = {scope: o.status for scope, o in run.scopes.items()}
    assert statuses == {
        "org": "complete",
        "members": "complete",
        "repos": "complete",
        "collaborators": "not_visible",
        "repo_settings": "complete",
    }
    assert sync_runs.last_complete(acme, "github", "collaborators") is None  # never seen whole
    members_seen = sync_runs.last_complete(acme, "github", "members")
    assert members_seen is not None
    hidden, _ = _api(["ana"], overrides={"/orgs/acme-example/members": [httpx.Response(404)]})
    again = GitHubLiveConnector("t", transport=httpx.MockTransport(hidden), sleep=lambda s: None)
    run = sync_connector(settings, acme, acme.tenant, again)
    assert run.status == "partial" and run.scopes["members"].status == "not_visible"
    # each scope keeps its own last complete sighting: members from the first pull only
    assert sync_runs.last_complete(acme, "github", "members") == members_seen
    collaborators_seen = sync_runs.last_complete(acme, "github", "collaborators")
    assert collaborators_seen is not None and collaborators_seen > members_seen


def test_a_connector_that_cannot_be_built_is_a_recorded_failed_run(
    own: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = own
    summary = ingest_tenant(settings, acme, acme.tenant, connectors=("hris",), source="live")
    (run,) = summary.runs
    assert run.status == "failed" and run.error_class == "RuntimeError" and run.scopes == {}
    assert sync_runs.recent(acme, 1)[0]["status"] == "failed"
