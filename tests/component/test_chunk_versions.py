"""MT9 (A-145): chunks are insert-only versions per evidence version, searchable as of a time,
and a re-embedding lands in a new generation that serves only after the retrieval gate.

The defects: the index kept current evidence only, updated in place, so a verdict could not
be replayed against what it searched; and a model change overwrote the serving vectors with
nothing measured first.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from ccp.config import Settings
from ccp.normalize import repository
from ccp.retrieval import generations, index, reembed
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.retrieval.hybrid import EmbeddingModelMismatch, retrieve
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

EMBEDDER = HashingEmbeddingClient()
DISCLOSURE = "vulnerability disclosure response timeline acknowledge report business days"
Q1_REVIEW = "hris:evidence:doc:access-review-2026-q1.md"


class Candidate(HashingEmbeddingClient):
    model_id = "local-hashing-v2"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_cver", ("acme",)) as (settings, dbs):
        yield settings, dbs["acme"]


def _now(db: ScopedDB) -> datetime:
    row = db.fetch_one("SELECT clock_timestamp()")
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def _count(db: ScopedDB, sql: str, params: Any = None) -> int:
    row = db.fetch_one(sql, params)
    return int(row[0]) if row else 0


def _top(db: ScopedDB, query: str, etype: str, **kw: Any) -> list[str]:
    result = retrieve(db, kw.pop("embedder", EMBEDDER), query, evidence_type=etype, **kw)
    return [c.text for c in result.chunks]


def test_every_document_gets_a_versioned_set_with_offsets(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    first = index.refresh(acme, settings, EMBEDDER, contextual_enrichment=False)
    assert first.serving and first.sets_written == first.evidence_rows > 0
    serving = generations.active(acme)
    assert serving is not None and serving.reason == "first"
    assert (
        serving.generation_id == first.generation_id == generations.generation_id(EMBEDDER.model_id)
    )
    served = _count(acme, "SELECT count(*) FROM chunks WHERE NOT is_distractor")
    assert _count(acme, "SELECT count(*) FROM chunk_versions") == served
    assert (
        _count(
            acme,
            "SELECT count(*) FROM chunks WHERE generation_id = %s AND start_offset IS NOT NULL",
            (first.generation_id,),
        )
        == served
    )
    sources = {e.entity_id: e for e in repository.current_evidence(acme)}
    for entity, page, start, end, text in acme.fetch_all(
        "SELECT evidence_entity_id, page, start_offset, end_offset, text FROM chunk_versions"
    ):
        row = sources[entity]
        source = row.page_texts[page - 1] if page is not None and row.page_texts else row.text
        assert "".join(source[start:end].split()) == "".join(text.split())

    again = index.refresh(acme, settings, EMBEDDER, contextual_enrichment=False)
    assert again.sets_written == 0 and again.upserted == 0 and again.tombstones == 0


def test_a_changed_document_adds_a_set_and_the_old_one_stays_searchable(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    before = _now(acme)
    old = acme.fetch_one(
        "SELECT chunk_id, evidence_entity_id FROM chunks WHERE text LIKE %s",
        ("%5 business days%",),
    )
    assert old is not None
    acme.execute(
        "INSERT INTO evidence (tenant_id, entity_id, observed_at, source_connector,"
        " source_event_id, evidence_type, title, text, subject_entity_id, issued_at, raw_ref,"
        " page_texts, guardrail_events)"
        " SELECT tenant_id, entity_id, clock_timestamp(), source_connector, 'evt-edited',"
        " evidence_type, title, replace(text, '5 business days', '3 business days'),"
        " subject_entity_id, issued_at, raw_ref, page_texts, guardrail_events"
        " FROM evidence WHERE entity_id = %s ORDER BY observed_at DESC LIMIT 1",
        (old[1],),
    )
    changed = index.refresh(acme, settings, EMBEDDER, contextual_enrichment=False)
    assert changed.sets_written == 1 and changed.upserted >= 1
    assert "3 business days" in _top(acme, DISCLOSURE, "policy_document")[0]
    assert "5 business days" in _top(acme, DISCLOSURE, "policy_document", as_of=before)[0]
    assert "3 business days" in _top(acme, DISCLOSURE, "policy_document", as_of=_now(acme))[0]
    sets = acme.fetch_all(
        "SELECT evidence_event_id FROM chunk_sets WHERE evidence_entity_id = %s"
        " ORDER BY recorded_at",
        (old[1],),
    )
    assert [r[0] for r in sets][-1] == "evt-edited" and len(sets) == 2
    # the chunk a verdict cited is no longer served, and still reads back exactly
    assert _count(acme, "SELECT count(*) FROM chunks WHERE chunk_id = %s", (old[0],)) == 0
    assert "5 business days" in generations.chunk_from_versions(acme, [old[0]])[old[0]]["text"]


def test_a_retired_document_gets_a_tombstone(
    world: tuple[Settings, ScopedDB], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, acme = world
    held = repository.current_evidence
    before = _now(acme)
    monkeypatch.setattr(
        index.repository,
        "current_evidence",
        lambda db, *a, **k: [e for e in held(db, *a, **k) if e.entity_id != Q1_REVIEW],
    )
    retired = index.refresh(acme, settings, EMBEDDER, contextual_enrichment=False)
    assert retired.tombstones == 1 and retired.sets_written == 0
    query = "Q1 2026 access review"
    assert not any("Q1 2026" in t for t in _top(acme, query, "access_review_report"))
    assert any("Q1 2026" in t for t in _top(acme, query, "access_review_report", as_of=before))
    monkeypatch.undo()
    back = index.refresh(acme, settings, EMBEDDER, contextual_enrichment=False)
    assert back.sets_written == 1 and back.tombstones == 0
    assert any("Q1 2026" in t for t in _top(acme, query, "access_review_report"))


def test_a_reembedding_serves_only_after_the_gate(world: tuple[Settings, ScopedDB]) -> None:
    settings, acme = world
    old_gen = generations.generation_id(EMBEDDER.model_id)
    served = _count(acme, "SELECT count(*) FROM chunks WHERE NOT is_distractor")
    trial = reembed.reembed(acme, settings, Candidate(), EMBEDDER, switch=False)
    assert trial.gate is not None and trial.gate.passed, trial.gate
    assert trial.gate.candidate["queries"] > 0 and not trial.switched
    assert trial.refresh.sets_written == trial.refresh.evidence_rows and not trial.refresh.serving
    assert index.stats(acme)["embed_models"] == {EMBEDDER.model_id: served}  # untouched
    with pytest.raises(EmbeddingModelMismatch, match="reembed"):
        retrieve(acme, Candidate(), DISCLOSURE, evidence_type="policy_document")

    before_switch = _now(acme)
    done = reembed.reembed(acme, settings, Candidate(), EMBEDDER)
    assert done.switched and done.projected == served and done.refresh.sets_written == 0
    serving = generations.active(acme)
    assert serving is not None and serving.reason == "gate_passed"
    assert serving.embed_model == Candidate.model_id
    assert index.stats(acme)["embed_models"] == {Candidate.model_id: served}
    assert "3 business days" in _top(acme, DISCLOSURE, "policy_document", embedder=Candidate())[0]
    # the old generation is kept: what was searched before the switch can still be searched
    old = next(g for g in generations.history(acme) if g["generation_id"] == old_gen)
    assert old["chunk_versions"] > 0 and not old["active"]
    assert _top(acme, DISCLOSURE, "policy_document", as_of=before_switch)
    with pytest.raises(EmbeddingModelMismatch):
        retrieve(acme, EMBEDDER, DISCLOSURE, evidence_type="policy_document", as_of=_now(acme))
    chained = _count(
        acme, "SELECT count(*) FROM audit_chain WHERE table_name = 'index_activations'"
    )
    assert chained == 2  # 'first' and 'gate_passed'
