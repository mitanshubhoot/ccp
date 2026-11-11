"""MT9 (A-146): agents read evidence exactly, not only by similarity.

`find_exact` finds literal strings and typed patterns (CVE, ARN, email, date, control id);
`outline` and `read_span` walk one document version and read it by offsets; `find_access`
answers "who holds what" as a typed join; and `search_evidence` gains subject, period and
as-of filters. Every result names its document version and offsets, and the Policy Agent's
reading tools stay pinned to policy documents.

The defects: semantic search is weak on exact strings and blind to structure, so an agent
could neither find "CVE-2026-1234" reliably nor read the section around a hit; and a
multi-hop question ("leavers who still hold write") had no tool but a chain of lookups.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ccp.agent.allowlists import ALLOWLISTS, POLICY_PINNED
from ccp.agent.authz import Gate
from ccp.agent.tools import TOOL_SPECS, BoundTool, ToolContext, bind_tools
from ccp.config import Settings
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.retrieval.rerank import LexicalOverlapReranker
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

EMBEDDER = HashingEmbeddingClient()
PDF = "manual:evidence:access-review-2026-h1.pdf"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB]]]:
    with prepared_tenants(base_settings, "ccp_reading") as (settings, dbs):
        for db in dbs.values():
            index.refresh(db, settings, EMBEDDER, contextual_enrichment=False)
        yield settings, dbs


def _tools(db: ScopedDB, graph: str = "copilot", **fixed: Any) -> dict[str, BoundTool]:
    ctx = ToolContext(db, EMBEDDER, LexicalOverlapReranker(), RetrievalConfig())
    return bind_tools(ctx, tuple(sorted(ALLOWLISTS[graph])), fixed, gate=Gate(f"graph:{graph}"))


def _ok(tool: BoundTool, args: dict[str, Any]) -> dict[str, Any]:
    out = tool.call(args)
    assert not out["is_error"], out
    TOOL_SPECS[tool.spec.name].output.model_validate(out["result"])
    return out["result"]  # type: ignore[no-any-return]


def test_an_exact_hit_reads_back_from_its_version(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = world[1]["acme"]
    tools = _tools(acme)
    found = _ok(
        tools["find_exact"], {"text": "5 business days", "evidence_type": "policy_document"}
    )
    assert found["total"] >= 1 and not found["truncated"]
    hit = found["matches"][0]
    assert hit["match"].lower() == "5 business days" and hit["version"]
    served = acme.fetch_one("SELECT text FROM chunks WHERE chunk_id = %s", (hit["chunk_id"],))
    assert served is not None and "5 business days" in served[0]
    span = _ok(
        tools["read_span"],
        {
            "evidence_type": "policy_document",
            "evidence_entity_id": hit["evidence_entity_id"],
            "start": hit["start_offset"],
            "end": hit["end_offset"],
            "version": hit["version"],
        },
    )
    assert span["text"] == hit["match"] and span["chunks"][0]["chunk_id"] == hit["chunk_id"]
    capped = _ok(tools["find_exact"], {"kind": "date", "limit": 1})
    assert capped["truncated"] and capped["total"] > 1 and len(capped["matches"]) == 1


def test_an_outline_leads_to_a_section_and_pages_are_respected(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    globex = world[1]["globex"]
    tools = _tools(globex)
    shape = _ok(
        tools["outline"],
        {"evidence_type": "access_review_report", "evidence_entity_id": _pdf_id(globex)},
    )
    assert shape["pages"] == 2 and shape["entries"] and shape["chunks"]
    served = {
        str(r[0])
        for r in globex.fetch_all(
            "SELECT chunk_id FROM chunks WHERE evidence_entity_id = %s", (_pdf_id(globex),)
        )
    }
    assert {c["chunk_id"] for c in shape["chunks"]} == served  # the ids the index holds
    table = next(e for e in shape["entries"] if e["title"].startswith("1. Purpose"))
    section = _ok(
        tools["read_span"],
        {
            "evidence_type": "access_review_report",
            "evidence_entity_id": _pdf_id(globex),
            "start": table["start_offset"],
            "end": table["end_offset"],
            "page": table["page"],
        },
    )
    assert section["text"].startswith("1. Purpose") and section["page"] == 1 == table["page"]
    assert section["chunks"]
    wrong_page = tools["read_span"].call(
        {
            "evidence_type": "access_review_report",
            "evidence_entity_id": _pdf_id(globex),
            "start": 0,
            "end": 10,
            "page": 9,
        }
    )
    assert wrong_page["is_error"] and "2 pages" in wrong_page["error"]
    other_type = tools["outline"].call(
        {"evidence_type": "policy_document", "evidence_entity_id": _pdf_id(globex)}
    )
    assert other_type["is_error"] and other_type["error"] == "not found"


def _pdf_id(db: ScopedDB) -> str:
    row = db.fetch_one(
        "SELECT DISTINCT evidence_entity_id FROM chunks WHERE strpos(evidence_entity_id, %s) = 1",
        (PDF,),
    )
    assert row is not None
    return str(row[0])


def test_search_takes_subject_period_and_as_of(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = world[1]["acme"]
    search = _tools(acme)["search_evidence"]
    base = {"query": "access review", "evidence_type": "access_review_report"}
    found = _ok(search, base)
    assert found["chunks"] and all(c["start_offset"] is not None for c in found["chunks"])
    future = search.call({**base, "issued_after": "2099-01-01"})
    assert future["is_error"] and "in that period" in future["error"]
    past = _ok(search, {**base, "issued_before": "2099-01-01"})
    assert {c["chunk_id"] for c in past["chunks"]} == {c["chunk_id"] for c in found["chunks"]}
    then = _ok(search, {**base, "as_of": datetime(2000, 1, 1, tzinfo=UTC).isoformat()})
    assert then["chunks"] == [] and "no_generation_as_of" in then["events"]
    subject = _ok(search, {**base, "subject_entity_id": "nobody"})
    assert subject["chunks"] == []


def test_typed_facts_answer_who_holds_what(world: tuple[Settings, dict[str, ScopedDB]]) -> None:
    acme = world[1]["acme"]
    find = _tools(acme)["find_access"]
    leavers = _ok(find, {"employment_status": "terminated", "asset_prefix": "github:"})
    assert leavers["rows"] and all(r["employment_status"] == "terminated" for r in leavers["rows"])
    assert any(r["display_name"] == "Jane Smith" for r in leavers["rows"])
    one = _ok(find, {"limit": 1})
    assert one["truncated"] and one["total"] > 1 and len(one["rows"]) == 1


def test_the_policy_agent_reads_policy_documents_only(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = world[1]["acme"]
    pinned = {name: {"evidence_type": "policy_document"} for name in POLICY_PINNED}
    tools = _tools(acme, "policy", **pinned)
    assert "find_access" not in tools and set(POLICY_PINNED) <= set(tools)
    refused = tools["find_exact"].call({"kind": "date", "evidence_type": "access_review_report"})
    assert refused["is_error"] and refused.get("fixed_argument") == "evidence_type"
    allowed = _ok(tools["find_exact"], {"text": "business days"})
    assert {m["evidence_type"] for m in allowed["matches"]} == {"policy_document"}
    review = acme.fetch_one(
        "SELECT evidence_entity_id FROM chunks WHERE evidence_type = 'access_review_report'"
    )
    assert review is not None
    blocked = tools["outline"].call({"evidence_entity_id": review[0]})
    assert blocked["is_error"] and blocked["error"] == "not found"
