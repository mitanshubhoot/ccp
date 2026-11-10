"""NT7 (A-102): the live GitHub client against a recorded API, no network.

- Transient errors and rate limits are retried and waited out; a persistent error stops the
  pull; a failed email lookup raises instead of reading as "no emails".
- A 404 on a listing marks the pull incomplete; an unchanged re-pull lands nothing.
- After a complete pull, a collaborator who is no longer listed is revoked as a new
  observation; after an incomplete pull, absence proves nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from ccp.config import Settings
from ccp.connectors.base import SourceEvent
from ccp.connectors.github.client import GitHubLiveConnector, GitHubPullError
from ccp.ingest.landing import land_events
from ccp.ingest.raw_zone import RawZone
from ccp.normalize.consumer import normalize_all
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

ORG = "acme-example"
REPO = f"{ORG}/platform"


def _api(
    collaborators: list[str],
    *,
    overrides: dict[str, list[httpx.Response]] | None = None,
    collaborators_status: int = 200,
) -> tuple[Callable[[httpx.Request], httpx.Response], list[str]]:
    """A recorded GitHub: one org, two members, one repo with the given collaborators.
    ``overrides`` serves queued responses for a path before the recorded one."""
    seen: list[str] = []
    queued = {k: list(v) for k, v in (overrides or {}).items()}

    def ok(body: Any) -> httpx.Response:
        return httpx.Response(200, json=body)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(f"{request.method} {path}")
        if queued.get(path):
            return queued[path].pop(0)
        if path == f"/orgs/{ORG}":
            return ok({"node_id": "O_1", "login": ORG, "updated_at": "2026-09-01T00:00:00Z"})
        if path == f"/orgs/{ORG}/members":
            if request.url.params.get("page", "1") != "1":
                return ok([])
            if request.url.params.get("role") == "admin":
                return ok([{"login": "ana", "node_id": "U_ANA"}])
            return ok([{"login": "ana", "node_id": "U_ANA"}, {"login": "bo", "node_id": "U_BO"}])
        if path == "/graphql":
            return ok({"data": {"user": {"organizationVerifiedDomainEmails": []}}})
        if path == f"/orgs/{ORG}/repos":
            if request.url.params.get("page", "1") != "1":
                return ok([])
            return ok(
                [
                    {
                        "full_name": REPO,
                        "name": "platform",
                        "node_id": "R_1",
                        "default_branch": "main",
                        "updated_at": "2026-09-01T00:00:00Z",
                    }
                ]
            )
        if path == f"/repos/{REPO}/collaborators":
            if collaborators_status != 200:
                return httpx.Response(collaborators_status, json={"message": "Not Found"})
            if request.url.params.get("page", "1") != "1":
                return ok([])
            return ok(
                [
                    {"login": login, "node_id": f"U_{login.upper()}", "role_name": "write"}
                    for login in collaborators
                ]
            )
        return httpx.Response(404, json={"message": "Not Found"})  # protection, SECURITY.md, files

    return handler, seen


def _pull(handler: Any, sleeps: list[float] | None = None) -> list[SourceEvent]:
    connector = GitHubLiveConnector(
        "t",
        transport=httpx.MockTransport(handler),
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
    )
    tenant = TenantContext.for_id(Settings(), "acme")
    return list(connector.pull(tenant))


def _completed(events: list[SourceEvent]) -> dict[str, Any]:
    (last,) = [e for e in events if e.event_type == "org.pull.completed"]
    assert events[-1] is last  # the marker closes the pull
    return last.payload


def test_transient_errors_and_rate_limits_are_waited_out() -> None:
    members = f"/orgs/{ORG}/members"
    handler, _ = _api(
        ["ana"],
        overrides={
            members: [
                httpx.Response(503),
                httpx.Response(403, headers={"x-ratelimit-remaining": "0", "retry-after": "7"}),
            ]
        },
    )
    sleeps: list[float] = []
    events = _pull(handler, sleeps)
    assert len(sleeps) == 2 and sleeps[1] == 7.0  # backoff, then the rate limit's own wait
    assert _completed(events)["complete"] is True


def test_a_persistent_error_stops_the_pull_and_a_failed_lookup_raises() -> None:
    members = f"/orgs/{ORG}/members"
    handler, _ = _api(["ana"], overrides={members: [httpx.Response(500)] * 5})
    with pytest.raises(GitHubPullError, match="gave up after 5 attempts"):
        _pull(handler)
    handler, _ = _api(
        ["ana"],
        overrides={"/graphql": [httpx.Response(200, json={"errors": [{"message": "scope"}]})]},
    )
    with pytest.raises(GitHubPullError, match="verified-email lookup"):
        _pull(handler)
    handler, _ = _api(["ana"], overrides={members: [httpx.Response(401)]})
    with pytest.raises(GitHubPullError, match="HTTP 401"):
        _pull(handler)


def test_a_listing_404_marks_the_pull_incomplete() -> None:
    handler, _ = _api(["ana"], collaborators_status=404)
    done = _completed(_pull(handler))
    assert done["complete"] is False and any("collaborators" in g for g in done["incomplete"])
    assert "R_1" not in done["repo_collaborators"]  # nothing may be inferred for that repo


def test_ids_come_from_content_so_an_unchanged_re_pull_is_the_same_events() -> None:
    handler, _ = _api(["ana"])
    first = [e.source_event_id for e in _pull(handler)]
    second = [e.source_event_id for e in _pull(handler)]
    assert first == second
    changed = [e.source_event_id for e in _pull(_api(["ana", "bo"])[0])]
    assert set(changed) != set(first)  # the collaborator change is a new repo snapshot


@pytest.fixture
def lone(base_settings: Settings, tmp_path: Path) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with scratch_database(base_settings, prefix="ccp_ghlive") as settings:
            local = settings.model_copy(update={"ccp_data_dir": tmp_path})
            pool = app_pool(local, max_size=2)
            try:
                yield local, ScopedDB(pool, TenantContext.for_id(local, "acme"))
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


def _grant_state(db: ScopedDB, login: str) -> tuple[str, datetime] | None:
    row = db.fetch_one(
        "SELECT state, observed_at FROM current_access_grants WHERE entity_id = %s",
        (f"github:grant:U_{login.upper()}:R_1",),
    )
    return (str(row[0]), row[1]) if row else None


def _land(settings: Settings, db: ScopedDB, events: list[SourceEvent]) -> int:
    landed, _ = land_events(db, RawZone(settings.raw_dir), events)
    normalize_all(db, settings)
    return landed


@pytest.mark.db
def test_a_removed_collaborator_is_revoked_only_after_a_complete_pull(
    lone: tuple[Settings, ScopedDB],
) -> None:
    settings, db = lone
    assert _land(settings, db, _pull(_api(["ana", "bo"])[0])) > 0
    assert _grant_state(db, "bo") is not None and _grant_state(db, "bo")[0] == "active"  # type: ignore[index]
    assert _land(settings, db, _pull(_api(["ana", "bo"])[0])) == 0  # unchanged: nothing lands
    _land(settings, db, _pull(_api(["ana"], collaborators_status=404)[0]))  # incomplete
    assert _grant_state(db, "bo")[0] == "active"  # type: ignore[index]  # absence proved nothing
    _land(settings, db, _pull(_api(["ana"])[0]))  # complete, and bo is gone
    state = _grant_state(db, "bo")
    assert state is not None and state[0] == "revoked"
    assert _grant_state(db, "ana")[0] == "active"  # type: ignore[index]
    history = db.fetch_all(
        "SELECT state FROM access_grants WHERE entity_id = %s ORDER BY observed_at",
        ("github:grant:U_BO:R_1",),
    )
    assert [r[0] for r in history] == ["active", "revoked"]  # a new observation, not an edit
    json.dumps([str(r) for r in history])
