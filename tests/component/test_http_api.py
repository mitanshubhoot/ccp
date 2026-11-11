"""MT4 (A-134): the HTTP API, signed in, per tenant, over the same services as the CLI.

The defect: everything a customer could do needed the command line on CCP's own machine,
with the tenant named by whoever typed it. Now each request proves who is asking with a
verified token, acts only in tenants that person belongs to, and needs the role the action
needs; a decision carries the hash of the payload the person was shown.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ccp import tenancy
from ccp.api.app import Pools, create_app
from ccp.approvals import store as approvals
from ccp.auth import dev_issuer
from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.remediation import fixes, suggestions
from ccp.storage.db import agent_pool, app_pool
from ccp.storage.scoped import ScopedDB
from tests.support.members import member
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, TestClient]]:
    with prepared_tenants(base_settings, "ccp_http", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1",))
        evaluations.drain(acme, settings, now=NOW)
        state_writer.drain(acme)
        tenancy.forget()
        pools = Pools(app_pool(settings, max_size=6), agent_pool(settings))
        try:
            with TestClient(create_app(settings, pools)) as client:
                yield settings, acme, client
        finally:
            pools.app.close()
            if pools.agent is not None:
                pools.agent.close()
            tenancy.forget()


def _as(settings: Settings, subject: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {dev_issuer.mint(settings, subject)}"}


def test_every_request_is_signed_in(world: tuple[Settings, ScopedDB, TestClient]) -> None:
    settings, _, client = world
    assert client.get("/v1/health").status_code == 200
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    me = client.get("/v1/me", headers=_as(settings, "nobody")).json()
    assert me["tenants"] == [] and me["principal_id"].endswith("#nobody")


def test_a_tenant_you_do_not_belong_to_does_not_exist(
    world: tuple[Settings, ScopedDB, TestClient],
) -> None:
    settings, acme, client = world
    stranger = _as(settings, "stranger")
    assert client.get("/v1/tenants/acme/approvals", headers=stranger).status_code == 404
    assert client.get("/v1/tenants/umbrella/approvals", headers=stranger).status_code == 404
    member(acme, "vera", "viewer")
    viewer = _as(settings, "vera")
    assert client.get("/v1/tenants/acme/findings", headers=viewer).status_code == 200
    assert client.get("/v1/tenants/acme/members", headers=viewer).status_code == 403
    me = client.get("/v1/me", headers=viewer).json()
    assert me["tenants"] == [{"tenant_id": "acme", "name": "Acme Corp", "role": "viewer"}]


def test_a_decision_over_http_names_the_payload_it_saw(
    world: tuple[Settings, ScopedDB, TestClient],
) -> None:
    settings, acme, client = world
    item_id = approvals.open_item(acme, "policy_draft", "draft-http", {"draft": "text"})
    member(acme, "vera", "viewer")
    member(acme, "ana", "approver")
    inbox = client.get("/v1/tenants/acme/approvals", headers=_as(settings, "vera")).json()
    shown = next(i for i in inbox["items"] if i["approval_id"] == item_id)
    url = f"/v1/tenants/acme/approvals/{item_id}/approve"
    body: dict[str, Any] = {"payload_sha256": shown["payload_sha256"]}
    assert client.post(url, json=body, headers=_as(settings, "vera")).status_code == 403
    stale = client.post(url, json={"payload_sha256": "0" * 64}, headers=_as(settings, "ana"))
    assert stale.status_code == 409
    done = client.post(url, json=body, headers=_as(settings, "ana"))
    assert done.status_code == 200 and done.json()["approval"]["state"] == "approved"
    again = client.post(url, json=body, headers=_as(settings, "ana"))
    assert again.status_code == 409
    history = client.get(f"/v1/tenants/acme/approvals/{item_id}", headers=_as(settings, "vera"))
    assert [h["event"] for h in history.json()["history"]] == ["opened", "approved"]


def test_a_design_partner_onboards_and_changes_scope_over_http(
    world: tuple[Settings, ScopedDB, TestClient],
) -> None:
    settings, _, client = world
    founder = _as(settings, "founder")
    made = client.post(
        "/v1/tenants",
        json={"tenant_id": "initech", "display_name": "Initech", "github_org": "initech-example"},
        headers=founder,
    )
    assert made.status_code == 201
    assert (
        client.post(
            "/v1/tenants",
            json={"tenant_id": "initech", "display_name": "x", "github_org": "x"},
            headers=founder,
        ).status_code
        == 409
    )
    shown = client.get("/v1/tenants/initech/settings", headers=founder).json()
    assert shown["settings"]["org_admin_limit"] == 2
    asked = client.patch(
        "/v1/tenants/initech/settings", json={"changes": {"org_admin_limit": 4}}, headers=founder
    )
    assert asked.status_code == 202
    item_id = asked.json()["approval_id"]
    granted = client.put(
        "/v1/tenants/initech/members",
        json={"issuer": dev_issuer.DEV_ISSUER, "subject": "bea", "role": "approver"},
        headers=founder,
    )
    assert granted.status_code == 200
    item = client.get(f"/v1/tenants/initech/approvals/{item_id}", headers=founder).json()
    url = f"/v1/tenants/initech/approvals/{item_id}/approve"
    body = {"payload_sha256": item["payload_sha256"]}
    assert client.post(url, json=body, headers=founder).status_code == 403  # asked for it
    assert client.post(url, json=body, headers=_as(settings, "bea")).status_code == 200
    now = client.get("/v1/tenants/initech/settings", headers=founder).json()
    assert now["settings"]["org_admin_limit"] == 4 and now["version"] != shown["version"]
    last = client.delete(
        "/v1/tenants/initech/members",
        params={"issuer": dev_issuer.DEV_ISSUER, "subject": "founder"},
        headers=founder,
    )
    assert last.status_code == 409  # a tenant keeps at least one admin


def test_reports_proposals_and_tools(world: tuple[Settings, ScopedDB, TestClient]) -> None:
    settings, acme, client = world
    member(acme, "rui", "reviewer")
    reviewer = _as(settings, "rui")
    report = client.get("/v1/tenants/acme/report/soc2", headers=reviewer)
    assert report.status_code == 200 and report.json()["requirements"]
    assert client.get("/v1/tenants/acme/report/nist", headers=reviewer).status_code == 404
    listed = client.get("/v1/tenants/acme/proposals", headers=reviewer)
    assert listed.status_code == 200
    tools = client.get("/v1/tenants/acme/tools", headers=reviewer)
    assert tools.status_code == 200 and tools.json() == {"tools": []}  # readers: watched only
    call = client.post(
        "/v1/tenants/acme/tools/get_control",
        json={"control_id": "CTL-ACCESS-REVOKE"},
        headers=reviewer,
    )
    assert call.status_code == 403 and "not permitted" in call.json()["error"]


def test_suggested_fixes_are_read_and_decided_over_http_but_never_applied(
    world: tuple[Settings, ScopedDB, TestClient],
) -> None:
    settings, acme, client = world
    member(acme, "rui", "reviewer")
    member(acme, "vic", "viewer")
    verdict = acme.fetch_one("SELECT verdict_id FROM verdicts WHERE test_id = 'HR-1' LIMIT 1")
    assert verdict is not None
    rendered = fixes.render(
        fixes.get("github_remove_collaborator"),
        {"login": "jsmith", "repo": "acme-example/platform"},
    )
    suggestion_id, _ = suggestions.suggest(
        acme,
        verdict_id=verdict[0],
        test_id="HR-1",
        resource="jsmith",
        source="model",
        rendered=rendered,
    )
    listed = client.get("/v1/tenants/acme/fixes", headers=_as(settings, "vic"))
    assert listed.status_code == 200
    assert [f["suggestion_id"] for f in listed.json()["fixes"]] == [suggestion_id]
    one = client.get(f"/v1/tenants/acme/fixes/{suggestion_id}", headers=_as(settings, "vic"))
    assert one.json()["body"]["cli"][0].startswith("gh api --method DELETE")
    path = f"/v1/tenants/acme/fixes/{suggestion_id}"
    assert client.post(f"{path}/accept", headers=_as(settings, "vic")).status_code == 403
    accepted = client.post(f"{path}/accept", headers=_as(settings, "rui"))
    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
    again = client.post(f"{path}/dismiss", params={"note": "  "}, headers=_as(settings, "rui"))
    assert again.status_code == 409  # a dismissal says why
    gone = client.post("/v1/tenants/acme/fixes/nope/accept", headers=_as(settings, "rui"))
    assert gone.status_code == 404
    numbers = client.get("/v1/tenants/acme/fix-metrics", headers=_as(settings, "rui"))
    assert numbers.json()["by_status"] == {"accepted": 1}
