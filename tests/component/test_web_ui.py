"""MT4 (A-135): the server-rendered UI, held to the inbox, findings, report, verdict and run
pages, with sign-in, and a Stop button that the run obeys at its next step.

The defects: a customer had no screen at all; nothing let a person stop an agent run; and a
status other than pass or fail said nothing about what would clear it.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from ulid import ULID

from ccp import auth, tenancy
from ccp.agent import stops, threads
from ccp.api import reasons
from ccp.api.app import Pools, create_app
from ccp.approvals import store as approvals
from ccp.auth import dev_issuer
from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.errors import RunStopped
from ccp.storage.db import app_pool
from ccp.storage.scoped import ScopedDB
from tests.support.members import member
from tests.support.pipeline import prepared_tenants

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


def test_a_reason_always_says_what_clears_it() -> None:
    known = reasons.for_events(["incomplete_source:github:members", "no_evidence_retrieved"])
    assert [r.next_action for r in known] == [
        "check the connection, then sync it",
        "upload the document for the period",
    ]
    unknown = reasons.for_events(["something_new"])
    assert unknown[0].code == "something_new" and unknown[0].next_action  # never hidden
    assert reasons.for_pending("inventory_stale").next_action == "sync the connection again"


def test_stop_ends_a_run_at_its_next_boundary() -> None:
    asked: list[str | None] = [None]
    tracker = BudgetTracker(RunBudget(1000, 1.0, 60.0), stop=lambda: asked[0])
    tracker.check_clock()  # nobody asked: it runs on
    asked[0] = "issuer#ana"
    with pytest.raises(RunStopped, match="stopped by issuer#ana"):
        tracker.check_clock()
    with pytest.raises(RunStopped):
        tracker.tool_call()  # a tool call is a boundary too


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, TestClient]]:
    with prepared_tenants(base_settings, "ccp_ui", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1",))
        evaluations.drain(acme, settings, now=NOW)
        state_writer.drain(acme)
        tenancy.forget()
        pools = Pools(app_pool(settings, max_size=6))
        try:
            with TestClient(create_app(settings, pools), follow_redirects=False) as client:
                yield settings, acme, client
        finally:
            pools.app.close()
            tenancy.forget()


def _sign_in(client: TestClient, subject: str) -> None:
    client.cookies.clear()
    answer = client.post("/ui/login", data={"subject": subject})
    assert answer.status_code == 303 and answer.headers["location"] == "/ui/"


def _csrf(page: str) -> str:
    return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]


@pytest.mark.db
def test_pages_need_a_session(world: tuple[Settings, ScopedDB, TestClient]) -> None:
    _, acme, client = world
    client.cookies.clear()
    gone = client.get("/ui/t/acme/inbox")
    assert gone.status_code == 303 and gone.headers["location"] == "/ui/login"
    _sign_in(client, "outsider")
    assert client.get("/ui/t/acme/inbox").status_code == 404  # not a member: no such tenant
    member(acme, "vera", "viewer")
    _sign_in(client, "vera")
    home = client.get("/ui/")
    assert home.status_code == 200 and "Acme Corp" in home.text


@pytest.mark.db
def test_the_inbox_decides_only_what_it_showed(
    world: tuple[Settings, ScopedDB, TestClient],
) -> None:
    settings, acme, client = world
    asker = member(acme, "olu", "approver")
    item_id = approvals.open_item(
        acme,
        "policy_draft",
        "draft-ui",
        {"draft": "access policy"},
        requested_by=asker.principal_id,
    )
    expired = approvals.open_item(
        acme, "policy_draft", "draft-old", {"draft": "old"}, expires_in=timedelta(seconds=-1)
    )
    _sign_in(client, "olu")
    mine = client.get("/ui/t/acme/inbox").text
    assert item_id in mine and "someone else decides it" in mine
    assert expired not in mine  # an expired item is not offered
    member(acme, "ana", "approver")
    _sign_in(client, "ana")
    page = client.get("/ui/t/acme/inbox").text
    shown = approvals.get(acme, item_id)
    assert shown is not None and shown.payload_sha256 in page
    url = f"/ui/t/acme/approvals/{item_id}/approve"
    forged = client.post(url, data={"csrf": "x" * 32, "payload_sha256": shown.payload_sha256})
    assert forged.status_code == 403
    stale = client.post(url, data={"csrf": _csrf(page), "payload_sha256": "0" * 64})
    assert stale.status_code == 303 and "refused" in stale.headers["location"]
    assert approvals.get(acme, item_id).state == "opened"  # type: ignore[union-attr]
    done = client.post(url, data={"csrf": _csrf(page), "payload_sha256": shown.payload_sha256})
    assert done.status_code == 303 and "approved" in done.headers["location"]
    assert approvals.get(acme, item_id).state == "approved"  # type: ignore[union-attr]


@pytest.mark.db
def test_findings_report_and_verdicts_render(world: tuple[Settings, ScopedDB, TestClient]) -> None:
    _, acme, client = world
    member(acme, "vera", "viewer")
    _sign_in(client, "vera")
    findings = client.get("/ui/t/acme/findings")
    assert findings.status_code == 200 and "HR-1|" in findings.text
    report = client.get("/ui/t/acme/report/soc2")
    assert report.status_code == 200 and "Requirement" in report.text
    assert client.get("/ui/t/acme/report/nist").status_code == 404
    verdict_id = findings.text.split("/verdicts/", 1)[1].split('"', 1)[0]
    page = client.get(f"/ui/t/acme/verdicts/{verdict_id}")
    assert page.status_code == 200 and "Why (written by the rule)" in page.text


def _a_run(acme: ScopedDB) -> str:
    thread_id = f"acme:investigator:{ULID()}"
    threads.create(acme, thread_id, "investigator", "HR-1", "HR-1|x")
    for seq, (node, kind, name) in enumerate(
        [("plan", "nonce", None), ("gather", "tool", "get_verdict"), ("gather", "model", None)]
    ):
        acme.execute(
            "INSERT INTO agent_steps (step_id, tenant_id, thread_id, seq, node, kind, call_key,"
            " request, response, model) VALUES (%s, 'acme', %s, %s, %s, %s, %s, %s, '{}', %s)",
            (
                str(ULID()),
                thread_id,
                seq,
                node,
                kind,
                f"k{seq}",
                json.dumps({"name": name} if name else {}),
                "claude-sonnet-5-5" if kind == "model" else None,
            ),
        )
    return thread_id


@pytest.mark.db
def test_a_run_page_streams_the_log_and_stops_the_run(
    world: tuple[Settings, ScopedDB, TestClient],
) -> None:
    _, acme, client = world
    thread_id = _a_run(acme)
    member(acme, "vera", "viewer")
    _sign_in(client, "vera")
    page = client.get(f"/ui/t/acme/runs/{thread_id}").text
    assert "get_verdict" in page and "Stop</button>" not in page  # a viewer cannot stop it
    member(acme, "rui", "reviewer")
    _sign_in(client, "rui")
    page = client.get(f"/ui/t/acme/runs/{thread_id}").text
    stopped = client.post(
        f"/ui/t/acme/runs/{thread_id}/stop", data={"csrf": _csrf(page), "reason": "wrong subject"}
    )
    assert stopped.status_code == 303 and "stop requested" in unquote_plus(
        stopped.headers["location"]
    )
    assert stops.requested(acme, thread_id) is not None
    threads.mark_cancelled(acme, thread_id, "run_stopped: stopped by rui", ["plan", "gather"])
    with client.stream("GET", f"/ui/t/acme/runs/{thread_id}/events") as events:
        body = "".join(events.iter_text())
    assert body.count("data: {") == 4 and "event: end" in body and '"cancelled"' in body


@pytest.mark.db
def test_browser_sign_in_with_an_identity_provider(
    world: tuple[Settings, ScopedDB, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, acme, _ = world
    oidc = settings.model_copy(
        update={
            "ccp_auth_mode": "oidc",
            "ccp_oidc_issuer": "https://idp.example.invalid",
            "ccp_oidc_authorize_url": "https://idp.example.invalid/oauth2/authorize",
            "ccp_oidc_token_url": "https://idp.example.invalid/oauth2/token",
        }
    )
    sent: list[dict[str, Any]] = []

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        sent.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(200, json={"id_token": dev_issuer.mint(settings, "ida")})

    real = auth.authenticate
    monkeypatch.setattr(auth, "authenticate", lambda s, t: real(settings, t))  # dev keys
    pools = Pools(app_pool(settings, max_size=2))
    http = httpx.Client(transport=httpx.MockTransport(token_endpoint))
    try:
        with TestClient(create_app(oidc, pools, http), follow_redirects=False) as client:
            start = client.get("/ui/login")
            assert start.status_code == 303
            asked = parse_qs(urlparse(start.headers["location"]).query)
            assert asked["code_challenge_method"] == ["S256"] and asked["response_type"] == ["code"]
            state = asked["state"][0]
            assert (
                client.get("/ui/callback", params={"code": "c", "state": "forged"}).status_code
                == 400
            )
            done = client.get("/ui/callback", params={"code": "c1", "state": state})
            assert done.status_code == 303 and "ccp_session" in done.cookies
            verifier = sent[0]["code_verifier"]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            assert challenge.rstrip(b"=").decode() == asked["code_challenge"][0]
            assert sent[0]["code"] == "c1" and sent[0]["grant_type"] == "authorization_code"
            assert client.post("/ui/login", data={"subject": "x"}).status_code == 404
    finally:
        pools.app.close()
        http.close()
