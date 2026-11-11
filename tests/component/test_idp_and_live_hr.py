"""MT6 (A-141): the identity provider, keyed by its immutable user ids, and live HR data
through an aggregator, both behind the sync contract.

The defects: CCP saw who could sign in only through the HR fixture, so a person still active
in the identity provider after leaving was invisible unless the HR system happened to list
the account; and live HR data could not be read at all.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from ccp.config import Settings
from ccp.connectors.base import SyncReport
from ccp.connectors.hris.aggregator import HrisAggregatorConnector, employee_payload
from ccp.connectors.idp.okta import OktaConnector
from ccp.evaluators import runner as evaluations
from ccp.evaluators.verdict import latest_per_subject
from ccp.identity import resolver
from ccp.ingest.landing import sync_connector
from ccp.ingest.raw_zone import RawZone
from ccp.normalize.consumer import normalize_all
from ccp.normalize.idp_mapper import map_idp_event
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from ccp.types import GrantState
from tests.support.pipeline import prepared_tenants

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
DOMAIN = "acme.okta.example"


def _okta(users: list[dict[str, Any]], factors: dict[str, list[dict[str, Any]]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"x-okta-request-id": f"okta-{request.url.path}"}
        if request.url.path == "/api/v1/users":
            if request.url.params.get("after") == "page2":
                return httpx.Response(200, json=users[1:], headers=headers)
            link = f'<https://{DOMAIN}/api/v1/users?after=page2&limit=200>; rel="next"'
            return httpx.Response(200, json=users[:1], headers={**headers, "link": link})
        user_id = request.url.path.split("/")[4]
        return httpx.Response(200, json=factors.get(user_id, []), headers=headers)

    return handler


def _user(
    user_id: str, email: str, status: str = "ACTIVE", login: str | None = None
) -> dict[str, Any]:
    first, _, last = email.split("@")[0].partition(".")
    return {
        "id": user_id,
        "status": status,
        "profile": {"login": login or email, "email": email, "firstName": first, "lastName": last},
    }


USERS = [
    _user("00uJANE", "jsmith@acme.example"),  # left the company, still active here
    _user("00uPRIYA", "pnatarajan@acme.example"),
    _user("00uGONE", "former@acme.example", status="DEPROVISIONED"),
]
FACTORS = {"00uPRIYA": [{"factorType": "webauthn", "status": "ACTIVE"}]}


def _connector(users: list[dict[str, Any]] = USERS) -> OktaConnector:
    return OktaConnector(
        DOMAIN, "t", transport=httpx.MockTransport(_okta(users, FACTORS)), clock=lambda: NOW
    )


def test_okta_pages_through_every_user_and_keys_them_by_id() -> None:
    connector = _connector()
    report = SyncReport(connector.scopes)
    events = list(connector.pull(TenantContext.for_id(Settings(), "acme"), None, report))
    assert [e.payload["user_id"] for e in events] == ["00uJANE", "00uPRIYA", "00uGONE"]
    assert report.status() == "complete"
    priya = next(e for e in events if e.payload["user_id"] == "00uPRIYA")
    assert priya.payload["mfa_enabled"] and priya.payload["factors"] == ["webauthn"]
    assert all(r.request_id for r in connector.responses)
    renamed = [_user("00uJANE", "jane.smith@acme.example", login="jane")] + USERS[1:]
    again = list(_connector(renamed).pull(TenantContext.for_id(Settings(), "acme")))
    person = {e.entity_id for e in map_idp_event(again[0]).entities}
    assert "idp:person:00uJANE" in person  # a renamed login is the same person
    gone = map_idp_event(events[2]).of(object)
    grant = next(x for x in gone if getattr(x, "role", None) == "sso")
    assert grant.state is GrantState.revoked  # deprovisioned: their access has ended


def test_a_failed_page_fails_the_scope_and_no_pass_rests_on_it() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("after") == "page2":
            return httpx.Response(500, json={"errorSummary": "boom"})
        return _okta(USERS, FACTORS)(request)

    connector = OktaConnector(DOMAIN, "t", transport=httpx.MockTransport(broken))
    report = SyncReport(connector.scopes)
    with pytest.raises(Exception, match="HTTP 500"):
        list(connector.pull(TenantContext.for_id(Settings(), "acme"), None, report))
    assert report.outcomes()["users"].status == "failed"


def test_the_aggregator_lands_the_same_employee_snapshot_as_the_fixture() -> None:
    pages = {
        None: {
            "next": "c2",
            "results": [
                {
                    "id": "m1",
                    "employee_number": "E7001",
                    "display_full_name": "Ada Lovelace",
                    "work_email": "ada@acme.example",
                    "employment_status": "ACTIVE",
                    "start_date": "2024-01-02T00:00:00Z",
                    "modified_at": "2026-09-01T00:00:00Z",
                }
            ],
        },
        "c2": {
            "next": None,
            "results": [
                {
                    "id": "m2",
                    "employee_number": None,
                    "first_name": "Ben",
                    "last_name": "Left",
                    "work_email": "ben@acme.example",
                    "employment_status": "INACTIVE",
                    "termination_date": "2026-08-15T00:00:00Z",
                    "modified_at": "2026-08-16T00:00:00Z",
                }
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Account-Token"] == "acct"
        return httpx.Response(
            200, json=pages[request.url.params.get("cursor")], headers={"x-request-id": "r"}
        )

    connector = HrisAggregatorConnector("key", "acct", transport=httpx.MockTransport(handler))
    report = SyncReport(connector.scopes)
    events = list(connector.pull(TenantContext.for_id(Settings(), "acme"), None, report))
    assert report.status() == "complete" and len(connector.responses) == 2
    ada, ben = (e.payload for e in events)
    assert ada["employee_id"] == "E7001" and ada["status"] == "active"
    assert ben["employee_id"] == "m2" and ben["status"] == "terminated"
    assert ben["terminated_at"] == "2026-08-15"
    assert events[0].event_type == "hris.employee.snapshot"
    assert employee_payload({"id": "x", "employment_status": "INACTIVE"})["status"] == "active"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_idp", ("acme",)) as (settings, dbs):
        yield settings, dbs["acme"]


@pytest.mark.db
def test_a_leaver_still_active_in_the_idp_fails_hr1_and_idp1_judges_mfa(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    run = sync_connector(settings, acme, acme.tenant, _connector(), zone=RawZone(settings.raw_dir))
    assert run.status == "complete", run.error
    normalize_all(acme, settings)
    resolver.resolve_tenant(acme)
    links = resolver.canonical_map(acme)
    assert links.get("idp:person:00uJANE") == "hris:person:E1042"  # linked by email
    evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1", "IDP-1"))
    evaluations.drain(acme, settings, now=NOW)
    verdicts = {v.subject_key: v for v in latest_per_subject(acme)}
    jane = verdicts["HR-1|person:hris:person:E1042"]
    assert jane.status.value == "fail" and "idp:asset:" in jane.reasoning
    idp1 = next(v for v in latest_per_subject(acme, "IDP-1"))
    assert idp1.status.value == "fail" and "jsmith@acme.example" in idp1.reasoning
    assert "former@acme.example" not in idp1.reasoning  # deprovisioned: not judged
    assert json.dumps(jane.evidence_refs)
