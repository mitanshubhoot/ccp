"""MT3 (A-116): one module reads secrets, and the write credential comes from a broker.

The defects: eleven call sites read secrets straight off the settings, so nothing bounded
who held one; and the remediation executor read GITHUB_TOKEN itself, so a code path that
reached it held GitHub's write credential with no approval behind it and no record. Now
`ccp/credentials.py` is the only reader, by declared purpose; the write credential has no
purpose there and comes only from the remediation broker, which checks the approval itself
and records every credential it issues; and the agent packages may not import either.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from ccp import credentials
from ccp.approvals import store as approvals
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators.verdict import latest_per_subject
from ccp.remediation import broker, gates, github_admin, proposals, runner
from ccp.remediation.github_admin import RecordingGitHubAdmin
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
JANE = "person:hris:person:E1042"
ALLOWED = gates.RemediationConfig(
    allowed_repos=["acme-example/platform"], protected_logins=["octocat"]
)


def _calls_named(name: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for path in (REPO / "ccp").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        n = sum(
            1
            for node in ast.walk(tree)
            if (isinstance(node, ast.Attribute) and node.attr == name)
            or (isinstance(node, ast.Name) and node.id == name)
            or (isinstance(node, ast.FunctionDef) and node.name == name)
        )
        if n:
            found[str(path.relative_to(REPO)).replace("\\", "/")] = n
    return found


def test_only_the_credential_module_reads_a_secret() -> None:
    assert set(_calls_named("get_secret_value")) == {"ccp/credentials.py"}


def test_only_the_broker_reaches_the_write_credential() -> None:
    assert set(_calls_named("remediation_token")) == {
        "ccp/credentials.py",
        "ccp/remediation/broker.py",
    }


def test_a_secret_is_read_by_declared_purpose() -> None:
    settings = Settings(anthropic_api_key=SecretStr("sk-ant-test"), voyage_api_key=None)
    assert credentials.secret(settings, "model:anthropic") == "sk-ant-test"
    assert credentials.secret(settings, "rerank:voyage") is None
    with pytest.raises(KeyError):  # the write credential has no purpose here
        credentials.secret(settings, "github:remediate")  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, str]]:
    with prepared_tenants(base_settings, "ccp_broker", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1",))
        evaluations.drain(acme, settings, now=NOW)
        verdict = {v.subject: v for v in latest_per_subject(acme, "HR-1")}[JANE]
        summary = runner.propose(acme, verdict)
        repo = next(p for p in summary.proposals if p.action == "remove_repo_collaborator")
        yield settings, acme, repo.proposal_id


def _fresh(acme: ScopedDB) -> str:
    verdict = {v.subject: v for v in latest_per_subject(acme, "HR-1")}[JANE]
    return proposals.create(
        acme,
        verdict_id=verdict.verdict_id,
        source="rule",
        action="remove_repo_collaborator",
        target={"repo": "acme-example/platform", "login": "jsmith"},
        rationale="test",
        reversible=True,
        live_capable=True,
    ).proposal_id


def _issues(acme: ScopedDB, operation: str) -> list[tuple[str, str]]:
    rows = acme.fetch_all(
        "SELECT action, approval_id FROM credential_issues WHERE operation = %s", (operation,)
    )
    return [(str(a), str(b)) for a, b in rows]


@pytest.mark.db
def test_the_broker_issues_only_for_an_approved_operation_and_records_it(world) -> None:  # type: ignore[no-untyped-def]
    settings, acme, _ = world
    with_token = settings.model_copy(update={"github_token": SecretStr("ghp_broker_test")})
    operation = _fresh(acme)
    item = approvals.for_ref(acme, "remediation", operation)
    assert item is not None and item.is_open
    scope = "github:acme-example/platform:remove_collaborator:jsmith"
    with pytest.raises(broker.NotApproved):  # still open: nothing is issued
        broker.issue(
            acme, with_token, operation=operation, action="execute", approval=item, scope=scope
        )
    decided = approvals.record_event(acme, item.approval_id, "approved", "security-lead")
    with pytest.raises(broker.NotApproved):  # an approval of another operation
        broker.issue(
            acme, with_token, operation="another", action="execute", approval=decided, scope=scope
        )
    assert _issues(acme, operation) == []
    credential = broker.issue(
        acme, with_token, operation=operation, action="execute", approval=decided, scope=scope
    )
    assert credential is not None and credential.token == "ghp_broker_test"
    assert "ghp_broker_test" not in repr(credential)
    assert credential.expires_at > credential.issued_at
    assert _issues(acme, operation) == [("execute", item.approval_id)]
    without = broker.issue(
        acme, settings, operation=operation, action="execute", approval=decided, scope=scope
    )
    assert without is None  # no token configured: nothing issued, nothing recorded
    assert len(_issues(acme, operation)) == 1


@pytest.mark.db
def test_the_executor_acts_only_with_the_brokers_credential(
    world: tuple[Settings, ScopedDB, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, acme, proposal_id = world
    item = approvals.for_ref(acme, "remediation", proposal_id)
    assert item is not None
    approvals.record_event(acme, item.approval_id, "approved", "security-lead", "confirmed")
    tokens: list[str] = []

    def live_admin(token: str) -> Any:
        tokens.append(token)
        return RecordingGitHubAdmin(permissions={("acme-example", "platform", "jsmith"): "write"})

    monkeypatch.setattr(github_admin, "LiveGitHubAdmin", live_admin)
    result = runner.execute(
        acme,
        settings.model_copy(
            update={"ccp_remediation_live": True, "github_token": SecretStr("ghp_live_test")}
        ),
        proposal_id,
        live=True,
        confirm=lambda prompt: "remove jsmith from acme-example/platform",
        config=ALLOWED,
    )
    assert result.mode == "executed"
    assert tokens == ["ghp_live_test"]
    assert _issues(acme, proposal_id) == [("execute", item.approval_id)]
