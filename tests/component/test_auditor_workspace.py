"""LT4 (A-169): an auditor reads named audit packages for a limited time, read-only, without
being a member of the tenant; every file read is logged; and everything CCP asserts outside
itself is judged under a versioned publication policy and recorded first.

The defects: nothing defined what CCP asserted outside itself, and an auditor could only
see the record by being given a member's access to everything.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ccp import tenancy
from ccp.api.app import Pools, create_app
from ccp.auth import dev_issuer
from ccp.compliance_state import attest, auditors, packages
from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.storage.db import agent_pool, app_pool
from ccp.storage.scoped import ScopedDB
from tests.support.members import member
from tests.support.pipeline import prepared_tenants

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


def test_the_policy_judges_provenance_age_and_completeness() -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    fresh = attest.Item("a", "system_observed", True, now - timedelta(days=3))
    upload = attest.Item("b", "customer_attested", True, now - timedelta(days=3))
    stale = attest.Item("c", "system_observed", True, now - timedelta(days=60))
    open_ = attest.Item("d", "ai", False, now)
    version, reasons = attest.judge("trust_page", [fresh], now)
    assert reasons == [] and len(version) == 12
    _, reasons = attest.judge("trust_page", [upload, stale], now)
    assert reasons == [
        "b: customer_attested may not go out as trust_page",
        "c: 60 days old; at most 31",
    ]
    _, reasons = attest.judge("questionnaire", [open_], now)
    assert reasons == ["d: ai may not go out as questionnaire", "d: not assessed"]
    _, reasons = attest.judge("package_share", [open_], now)
    assert reasons == []  # an auditor sees unassessed and AI results, marked by their method
    assert attest.judge("press_release", [fresh], now)[1] == [
        "no publication rule for 'press_release'"
    ]


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, TestClient, str]]:
    with prepared_tenants(base_settings, "ccp_auditors", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1",))
        evaluations.drain(acme, settings, now=NOW)
        state_writer.drain(acme)
        package_id, _ = packages.build(acme, settings, "soc2", NOW + timedelta(hours=1), by="sam")
        tenancy.forget()
        pools = Pools(app_pool(settings, max_size=6), agent_pool(settings))
        try:
            with TestClient(create_app(settings, pools)) as client:
                yield settings, acme, client, package_id
        finally:
            pools.app.close()
            if pools.agent is not None:
                pools.agent.close()
            tenancy.forget()


def _as(settings: Settings, subject: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {dev_issuer.mint(settings, subject)}"}


def _principal(settings: Settings, subject: str) -> str:
    from ccp import auth

    return auth.authenticate(settings, dev_issuer.mint(settings, subject)).principal_id


@pytest.mark.db
def test_an_auditor_reads_named_packages_until_the_grant_ends(
    world: tuple[Settings, ScopedDB, TestClient, str],
) -> None:
    settings, acme, client, package_id = world
    member(acme, "ada", "admin")
    member(acme, "rui", "reviewer")
    auditor = _principal(settings, "audrey")
    body: dict[str, Any] = {"principal_id": auditor, "package_ids": [package_id], "days": 14}
    assert (
        client.post(
            "/v1/tenants/acme/auditor-grants", json=body, headers=_as(settings, "rui")
        ).status_code
        == 403
    )
    assert (
        client.get("/v1/tenants/acme/auditor/packages", headers=_as(settings, "audrey")).status_code
        == 404
    )
    made = client.post("/v1/tenants/acme/auditor-grants", json=body, headers=_as(settings, "ada"))
    assert made.status_code == 201, made.text
    grant_id = made.json()["grant"]["grant_id"]
    shared = acme.fetch_one(
        "SELECT kind, allowed, policy_version FROM attestations WHERE attestation_id = %s",
        (made.json()["attestation_id"],),
    )
    assert shared is not None and shared[0] == "package_share" and shared[1] is True

    listed = client.get("/v1/tenants/acme/auditor/packages", headers=_as(settings, "audrey"))
    assert listed.status_code == 200
    (summary,) = listed.json()["packages"]
    assert summary["package_id"] == package_id and "manifest.json" in summary["files"]
    path = f"/v1/tenants/acme/auditor/packages/{package_id}/files/results.json"
    read = client.get(path, headers=_as(settings, "audrey"))
    assert read.status_code == 200 and json.loads(read.content)
    assert client.get(path, headers=_as(settings, "mallory")).status_code == 404
    missing = f"/v1/tenants/acme/auditor/packages/{package_id}/files/secrets.txt"
    assert client.get(missing, headers=_as(settings, "audrey")).status_code == 404
    logged = acme.fetch_all("SELECT file, grant_id FROM auditor_access")
    assert ("results.json", grant_id) in [(r[0], r[1]) for r in logged]

    assert auditors.visible(acme, auditor, datetime.now(UTC) + timedelta(days=15)) == {}  # expired
    with pytest.raises(ValueError, match="1 to 90"):
        auditors.grant(acme, auditor, [package_id], 365, "ada", None)
    revoked = client.post(
        f"/v1/tenants/acme/auditor-grants/{grant_id}/revoke",
        params={"reason": "fieldwork ended"},
        headers=_as(settings, "ada"),
    )
    assert revoked.status_code == 200
    assert client.get(path, headers=_as(settings, "audrey")).status_code == 404
