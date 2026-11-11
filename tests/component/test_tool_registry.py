"""MT3 (A-112): the typed tool registry and its gateway.

The defects: a tool declared only its arguments, so nothing checked what it returned, said
whether it reads or acts, or bounded how often a run may call it; and absent data came back
as an empty list ("findings": []), which reads as "nothing is wrong" when nothing was ever
assessed. Now every tool declares an output schema, a class, scopes, a call limit, a time
limit and a version; the gateway checks every result; and absent data is an error that names
what is missing.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from ccp.agent import bundle, tools
from ccp.agent.authz import Gate, Rule
from ccp.agent.tools import TOOL_SPECS, BoundTool, ToolContext, ToolSpec, bind_tools
from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.retrieval.rerank import LexicalOverlapReranker
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from tests.support.pipeline import prepared_tenants

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


def _ctx(db: ScopedDB) -> ToolContext:
    return ToolContext(db, HashingEmbeddingClient(), LexicalOverlapReranker(), RetrievalConfig())


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[ScopedDB, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_registry", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        index.refresh(acme, settings, HashingEmbeddingClient(), contextual_enrichment=False)
        evaluations.enqueue_all(acme, settings, now=NOW, tiers=(1,))
        evaluations.drain(acme, settings, now=NOW)
        state_writer.drain(acme)
        # globex has no data at all in this database: nothing pulled, nothing assessed
        empty = ScopedDB(acme._pool, TenantContext.for_id(settings, "globex"))
        yield acme, empty


def test_every_tool_declares_its_whole_contract() -> None:
    for name, spec in TOOL_SPECS.items():
        contract = spec.contract()
        assert contract["class"] in ("read", "propose", "act"), name
        assert contract["output_schema"]["type"] == "object", name
        assert spec.max_calls >= 1 and spec.timeout_seconds > 0 and spec.version, name
    # CCP's agents hold no tool that changes anything
    assert {s.cls for s in TOOL_SPECS.values()} == {"read"}


def test_the_bundle_hash_covers_the_whole_declaration(monkeypatch: pytest.MonkeyPatch) -> None:
    before = bundle.parts()
    spec = TOOL_SPECS["get_control"]
    monkeypatch.setitem(TOOL_SPECS, "get_control", ToolSpec(**{**spec.__dict__, "version": "2"}))
    assert bundle.parts() != before  # a new version is a new bundle, as a new schema is


@pytest.mark.db
def test_every_result_matches_its_output_schema(world: tuple[ScopedDB, ScopedDB]) -> None:
    acme, _ = world
    bound = bind_tools(_ctx(acme), tuple(sorted(TOOL_SPECS)), gate=Gate("graph:copilot"))
    chunk = acme.fetch_one(
        "SELECT chunk_id, evidence_entity_id, evidence_type, page FROM chunks LIMIT 1"
    )
    verdict = acme.fetch_one("SELECT verdict_id, test_id, subject_key FROM verdicts LIMIT 1")
    person = acme.fetch_one("SELECT entity_id FROM persons LIMIT 1")
    assert chunk is not None and verdict is not None and person is not None
    calls: dict[str, dict[str, Any]] = {
        "search_evidence": {"query": "access review", "evidence_type": "access_review_report"},
        "get_chunk": {"chunk_id": chunk[0]},
        "list_evidence": {"evidence_type": "access_review_report"},
        "get_evidence_chunks": {"evidence_entity_id": chunk[1]},
        "find_exact": {"kind": "date"},
        "outline": {"evidence_type": chunk[2], "evidence_entity_id": chunk[1]},
        "read_span": {
            "evidence_type": chunk[2],
            "evidence_entity_id": chunk[1],
            "start": 0,
            "end": 80,
            "page": chunk[3],
        },
        "find_access": {"state": "active"},
        "list_assets": {},
        "get_person": {"canonical_person_id": person[0]},
        "get_current_state": {"test_id": verdict[1], "subject_key": verdict[2]},
        "get_verdict": {"verdict_id": verdict[0]},
        "list_findings": {},
        "get_access_grants": {"canonical_person_id": person[0]},
        "get_control": {"control_id": "CTL-ACCESS-REVOKE"},
        "get_framework_rollup": {"framework_id": "soc2"},
        "get_investigation": {"verdict_id": verdict[0]},
        "get_lookups": {"verdict_id": verdict[0]},  # A-158: none stored, an empty list
        "read_result": {"handle": "acme:registry:stored", "max_chars": 200},  # A-153
    }
    acme.execute(
        "INSERT INTO tool_results (tenant_id, handle, thread_id, tool, content, chars, tokens)"
        " VALUES ('acme', 'acme:registry:stored', 'acme:registry', 'search_evidence', %s, 600,"
        " 150) ON CONFLICT DO NOTHING",
        ("x" * 600,),
    )
    assert set(calls) == set(TOOL_SPECS)
    for name, args in calls.items():
        result = bound[name].call(args)
        assert not result.get("malformed_result"), name
        if not result["is_error"]:
            TOOL_SPECS[name].output.model_validate(result["result"])
        elif name != "get_investigation":  # no investigation was run in this world
            raise AssertionError(f"{name}: {result['error']}")


@pytest.mark.db
def test_absent_data_is_an_error_that_names_it_never_an_empty_list(
    world: tuple[ScopedDB, ScopedDB],
) -> None:
    _, empty = world
    bound = bind_tools(_ctx(empty), tuple(sorted(TOOL_SPECS)), gate=Gate("graph:copilot"))
    expected = {
        "list_findings": ({}, "evaluations"),
        "list_assets": ({}, "assets"),
        "list_evidence": ({"evidence_type": "access_review_report"}, "access_review_report"),
        "search_evidence": (
            {"query": "review", "evidence_type": "access_review_report"},
            "access_review_report",
        ),
        "get_current_state": ({"test_id": "HR-1", "subject_key": "tenant"}, "evaluation"),
        "get_access_grants": ({"canonical_person_id": "person:hris:person:E1"}, "grants"),
        "get_evidence_chunks": ({"evidence_entity_id": "evidence:none"}, "chunks"),
        "find_exact": ({"text": "review"}, "evidence"),
        "find_access": ({}, "grants"),
    }
    for name, (args, what) in expected.items():
        result = bound[name].call(args)
        assert result["is_error"] and result["missing"] == what, (name, result)


def test_the_gateway_refuses_a_malformed_result_and_counts_calls() -> None:
    class Out(BaseModel):
        value: int

    spec = ToolSpec("probe", "a probe", tools.GetControlArgs, Out, max_calls=2)
    replies: list[Any] = [{"value": "not a number"}, {"value": 3}, {"value": 4}]
    probe = BoundTool(
        spec,
        lambda ctx, args: replies.pop(0),
        _ctx(None),  # type: ignore[arg-type]
        gate=Gate("test:probe", table=(Rule(id="t", principal="test:probe", tools=("probe",)),)),
    )
    bad = probe.call({"control_id": "x"})
    assert bad == {
        "is_error": True,
        "error": "probe returned a malformed result",
        "malformed_result": True,
    }
    assert probe.call({"control_id": "x"}) == {"is_error": False, "result": {"value": 3}}
    limited = probe.call({"control_id": "x"})  # the third call of a two-call tool
    assert limited["is_error"] and limited["rate_limited"]
