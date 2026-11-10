"""MT3 (A-117): the REST surface is generated from the tool registry and goes through the
same gateway as the agents.

The defect: the tools existed only inside the agents. Nothing outside CCP could reach them
under one permission check, and nothing described them for a client. Now the registry
generates the routes and an OpenAPI document; every call is decided by policy.check under
the caller's principal and logged with the caller's user, and a new rule for API readers is
watched before it permits.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from typer.testing import CliRunner

from ccp import cli
from ccp.agent import authz, rest
from ccp.agent.authz import Rule
from ccp.agent.bundle import bundle_sha256
from ccp.agent.rest import Caller
from ccp.agent.tools import TOOL_SPECS, ControlOut
from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators.tier2 import Providers
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.db import agent_pool
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from tests.support.pipeline import prepared_tenants

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
READERS = (
    "list_findings",
    "get_current_state",
    "get_verdict",
    "get_control",
    "get_framework_rollup",
)
ENFORCED = authz.base_rules() + (Rule(id="api-readers", principal="api:reader", tools=READERS),)
CONTROL = json.dumps({"control_id": "CTL-ACCESS-REVOKE"}).encode()


def _refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        own = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return own + [r for v in node.values() for r in _refs(v)]
    if isinstance(node, list):
        return [r for v in node for r in _refs(v)]
    return []


def test_the_document_carries_every_tool_with_its_own_schemas() -> None:
    doc = rest.openapi()
    assert doc["openapi"] == "3.1.0" and doc["info"]["version"] == bundle_sha256()[:12]
    for name, spec in TOOL_SPECS.items():
        post = doc["paths"][f"/v1/tools/{name}"]["post"]
        assert post["x-ccp-class"] == spec.cls and post["summary"] == spec.description
        assert {"200", "403", "404", "422", "429", "500"} <= set(post["responses"])
    schemas = doc["components"]["schemas"]
    for ref in _refs(doc):
        assert ref.startswith("#/components/schemas/") and ref.rsplit("/", 1)[1] in schemas, ref


def test_the_document_comes_out_of_the_command_line() -> None:
    result = CliRunner().invoke(cli.app, ["api", "openapi"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["openapi"] == "3.1.0"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[dict[str, Any]]:
    with prepared_tenants(base_settings, "ccp_rest", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        evaluations.enqueue_all(acme, settings, now=NOW, tiers=(1,))
        evaluations.drain(acme, settings, now=NOW)
        state_writer.drain(acme)
        pool = agent_pool(settings)
        try:
            yield {
                "log_db": acme,
                "read_db": ScopedDB(pool, TenantContext.for_id(settings, "acme")),
                "providers": Providers(HashingEmbeddingClient(), None, None, RetrievalConfig()),
            }
        finally:
            pool.close()


@pytest.mark.db
def test_a_new_reader_rule_is_watched_before_it_permits(world: dict[str, Any]) -> None:
    status, body = rest.handle(
        Caller("reader", "u-watch"), "POST", "/v1/tools/get_control", CONTROL, **world
    )
    assert status == 403 and body["policy"] == "deny"
    row = world["log_db"].fetch_one(
        "SELECT effect, shadow_rule_id, principal, tool FROM policy_decisions"
        " WHERE user_id = 'u-watch'"
    )
    assert row == ("deny", "api-readers", "api:reader", "get_control")
    listing = rest.handle(Caller("reader", "u-watch"), "GET", "/v1/tools", None, **world)
    assert listing == (200, {"tools": []})  # nothing is offered while the rule is watched


@pytest.mark.db
def test_an_enforced_reader_gets_results_in_the_tools_own_schema(world: dict[str, Any]) -> None:
    caller = Caller("reader", "u-read")
    status, body = rest.handle(
        caller, "POST", "/v1/tools/get_control", CONTROL, table=ENFORCED, **world
    )
    assert status == 200
    ControlOut.model_validate(body["result"])
    listing = rest.handle(caller, "GET", "/v1/tools", None, table=ENFORCED, **world)
    assert listing[0] == 200
    assert sorted(t["name"] for t in listing[1]["tools"]) == sorted(READERS)


@pytest.mark.db
def test_the_gateways_answers_map_onto_http(world: dict[str, Any]) -> None:
    caller = Caller("reader", "u-codes")

    def call(path: str, body: bytes | None, method: str = "POST") -> tuple[int, dict[str, Any]]:
        return rest.handle(caller, method, path, body, table=ENFORCED, **world)

    assert call("/v1/tools/get_control", b'{"control_id": 7}')[0] == 422
    assert call("/v1/tools/get_control", b"not json")[0] == 422
    assert call("/v1/tools/delete_repo", b"{}")[0] == 404
    assert call("/v1/tools/get_control", None, method="GET")[0] == 405
    refused = call("/v1/tools/search_evidence", b'{"query": "q", "evidence_type": "x"}')
    assert refused[0] == 403  # not a reader's tool: refused and logged
    missing = call("/v1/tools/get_current_state", b'{"test_id": "HR-1", "subject_key": "nobody"}')
    assert missing[0] == 404 and missing[1]["missing"] == "evaluation"
    logged = world["log_db"].fetch_one(
        "SELECT count(*) FROM policy_decisions WHERE user_id = 'u-codes'"
    )
    # every call that reached the gateway: the invalid one, the refused one, the missing one
    assert logged == (3,)
