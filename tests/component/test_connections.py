"""MT1 (A-121): connections as data, checked daily.

The defects: `pull()` was the whole contract. Nothing recorded which provider account a
tenant was connected to, or with which credential, so a pull named no connection; nothing
probed a credential until a pull failed on it; and nothing could say that a connection was
refused, hidden, stale or degraded.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from ccp import credentials
from ccp.config import ConnectionSpec, Settings
from ccp.connectors.base import Connection, Connector
from ccp.connectors.github.client import GitHubLiveConnector
from ccp.connectors.registry import build_connector
from ccp.evaluators import jobs
from ccp.ingest import connections
from ccp.ingest.landing import ingest_tenant
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext, TenantId
from tests.component.test_github_recorded import ORG, _api

pytestmark = pytest.mark.db

FIXTURES = {"github-fixture-acme-example", "hris-fixture-acme", "manual-fixture-acme"}
LIVE = "github-live-acme-example"


@pytest.fixture
def own(base_settings: Settings, tmp_path: Path) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with scratch_database(base_settings, prefix="ccp_conn") as settings:
            local = settings.model_copy(update={"ccp_data_dir": tmp_path})
            pool = app_pool(local, max_size=2)
            try:
                yield local, ScopedDB(pool, TenantContext.for_id(local, "acme"))
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


def _declaring(db: ScopedDB, *specs: ConnectionSpec) -> ScopedDB:
    """The same tenant, declaring other connections."""
    tenant = db.tenant
    settings = tenant.settings.model_copy(update={"connections": specs})
    return ScopedDB(db._pool, TenantContext(TenantId(tenant.tenant_id), settings))


def _live_github(status: int | None = None) -> Any:
    """A connector factory whose live GitHub client talks to the recorded API, refusing the
    credential with `status` when one is given."""

    def build(
        name: Any, settings: Settings, source: Any = "fixture", connection: Any = None
    ) -> Connector:
        if (name, source) != ("github", "live"):
            return build_connector(name, settings, source, connection)
        refused = {f"/orgs/{ORG}": [httpx.Response(status, json={"message": "Bad credentials"})]}
        handler, _ = _api(["ana"], overrides=refused if status else None)
        return GitHubLiveConnector(
            credentials.secret(settings, "github:read") or "t",  # the token reaches the client
            transport=httpx.MockTransport(handler),
            sleep=lambda s: None,
            org=connection.account if connection else None,
            connection_id=connection.connection_id if connection else None,
        )

    return build


def test_declared_connections_become_versioned_rows(own: tuple[Settings, ScopedDB]) -> None:
    _, acme = own
    assert connections.sync(acme) == 3  # the defaults: three fixture connections
    assert connections.sync(acme) == 0  # in line already: nothing appended
    assert {c.connection_id for c in connections.enabled(acme)} == FIXTURES
    live = _declaring(
        acme,
        ConnectionSpec(connector="github", source="live", credential="github:read"),
        ConnectionSpec(connector="hris"),
    )
    assert connections.sync(live) == 3  # the live one added; github and manual fixtures dropped
    now = {c.connection_id: c for c in connections.current(live)}
    assert now[LIVE].enabled and now[LIVE].account == "acme-example"
    assert now[LIVE].credential == "github:read"
    assert not now["github-fixture-acme-example"].enabled  # disabled, never deleted
    assert now["github-fixture-acme-example"].version == 2
    history = acme.fetch_all(
        "SELECT version, enabled FROM connections WHERE connection_id = %s ORDER BY version",
        ("github-fixture-acme-example",),
    )
    assert [tuple(r) for r in history] == [(1, True), (2, False)]


def test_every_pull_names_its_connection(own: tuple[Settings, ScopedDB]) -> None:
    settings, acme = own
    summary = ingest_tenant(settings, acme, acme.tenant)
    named = acme.fetch_all("SELECT connector, connection_id FROM sync_runs ORDER BY connector")
    assert [tuple(r) for r in named] == [
        ("github", "github-fixture-acme-example"),
        ("hris", "hris-fixture-acme"),
        ("manual", "manual-fixture-acme"),
    ]
    assert all(r.status == "complete" for r in summary.runs)
    # a source no connection declares still pulls, and is recorded with no connection
    ingest_tenant(settings, acme, acme.tenant, connectors=("hris",), source="live")
    (orphan,) = acme.fetch_all("SELECT connection_id FROM sync_runs WHERE source = 'live'")
    assert orphan[0] is None


def test_the_daily_jobs_probe_then_pull_every_enabled_connection(
    own: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = own
    slot = datetime.now(UTC)
    assert {c.connection_id: c.state for c in _health(settings, acme)} == dict.fromkeys(
        FIXTURES, "stale"
    )  # declared, never pulled
    assert jobs.JOBS["check_connections"](acme, settings, slot) == 3
    assert jobs.JOBS["sync"](acme, settings, slot) == 3
    assert {h.connection_id: h.state for h in _health(settings, acme)} == dict.fromkeys(
        FIXTURES, "healthy"
    )
    later = datetime.now(UTC) + timedelta(hours=settings.ccp_sync_stale_hours + 1)
    stale = [connections.health(acme, settings, c, later) for c in connections.current(acme)]
    assert {h.state for h in stale} == {"stale"}  # a day with no complete pull


def test_a_refused_credential_shows_as_auth_failed(own: tuple[Settings, ScopedDB]) -> None:
    settings, acme = own
    live = _declaring(
        acme, ConnectionSpec(connector="github", source="live", credential="github:read")
    )
    connections.sync(live)
    (connection,) = connections.enabled(live)
    ok = connections.check(settings, live, connection, _live_github())
    assert ok.outcome == "ok"
    refused = connections.check(settings, live, connection, _live_github(status=401))
    assert refused.outcome == "auth" and "HTTP 401" in refused.detail
    health = connections.health(live, settings, connection)
    assert health.state == "auth_failed" and health.last_check == refused
    checks = live.fetch_all(
        "SELECT outcome FROM connection_checks WHERE connection_id = %s ORDER BY checked_at",
        (LIVE,),
    )
    assert [r[0] for r in checks] == ["ok", "auth"]  # every probe is kept


def test_a_live_connection_with_no_credential_is_refused_at_its_check(
    own: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = own
    keyless = settings.model_copy(update={"github_token": None})
    live = _declaring(
        acme, ConnectionSpec(connector="github", source="live", credential="github:read")
    )
    connections.sync(live)
    (connection,) = connections.enabled(live)
    check = connections.check(keyless, live, connection)
    assert check.outcome == "auth" and "GITHUB_TOKEN" in check.detail


class _Health:
    def __init__(self, h: connections.Health) -> None:
        self.connection_id = h.connection.connection_id
        self.state = h.state


def _health(settings: Settings, db: ScopedDB) -> list[_Health]:
    connections.sync(db)
    return [_Health(connections.health(db, settings, c)) for c in connections.current(db)]


def test_the_connection_record_holds_no_secret(own: tuple[Settings, ScopedDB]) -> None:
    settings, acme = own
    secret = settings.model_copy(update={"github_token": SecretStr("ghp_never_stored")})
    live = _declaring(
        acme, ConnectionSpec(connector="github", source="live", credential="github:read")
    )
    connections.sync(live)
    (connection,) = connections.enabled(live)
    assert isinstance(connection, Connection) and connection.credential == "github:read"
    connections.check(secret, live, connection, _live_github(status=401))
    dumped = live.fetch_all("SELECT * FROM connections") + live.fetch_all(
        "SELECT * FROM connection_checks"
    )
    assert "ghp_never_stored" not in repr(dumped)
