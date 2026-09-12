"""The retrieval index and the hybrid query against a scratch database: page-aware chunks,
the short-result fix under filters, model-mismatch refusal, RLS on chunks without ScopedDB
(S1 b), provider degradation, distractors, metrics, calibration."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import psycopg
import pytest

from ccp.config import Settings
from ccp.llm.errors import RetryableModelError
from ccp.retrieval import calibration, distractors, index, metrics
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import (
    EmbeddingResult,
    HashingEmbeddingClient,
    InputType,
    vector_literal,
)
from ccp.retrieval.hybrid import EmbeddingModelMismatch, retrieve
from ccp.retrieval.rerank import LexicalOverlapReranker
from ccp.storage.db import app_role_and_password
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

EMBEDDER = HashingEmbeddingClient()
CONFIG = RetrievalConfig()


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, dict[str, ScopedDB]]]:
    with prepared_tenants(base_settings, "ccp_retr") as (settings, dbs):
        for db in dbs.values():
            index.refresh(db, settings, EMBEDDER, contextual_enrichment=False)
        distractors.install(dbs["acme"], EMBEDDER, 300)
        yield settings, dbs


def test_index_is_content_addressed_and_idempotent(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    settings, dbs = world
    acme = dbs["acme"]
    first = index.stats(acme)
    assert first["chunks_by_type"]["policy_document"] >= 2 and first["embed_models"] == {
        "local-hashing-v1": sum(first["chunks_by_type"].values()) + first["distractors"]
    }
    again = index.refresh(acme, settings, EMBEDDER, contextual_enrichment=False)
    assert again.upserted == 0 and again.unchanged == again.chunks and again.deleted == 0
    rebuilt = index.refresh(acme, settings, EMBEDDER, contextual_enrichment=False, rebuild=True)
    assert (
        rebuilt.upserted == rebuilt.chunks and rebuilt.embedded == 0
    )  # every vector came from the cache
    assert rebuilt.embedding_cache_hits == rebuilt.chunks
    assert index.stats(acme)["distractors"] == first["distractors"]


def test_split_table_pdf_chunks_know_their_pages(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    globex = world[1]["globex"]
    rows = globex.fetch_all(
        "SELECT page, text FROM chunks WHERE strpos(evidence_entity_id, %s) = 1 ORDER BY ordinal",
        ("manual:evidence:access-review-2026-h1.pdf",),
    )
    pages = {int(r[0]) for r in rows}
    assert pages == {1, 2}
    assert all(int(r[0]) == 1 for r in rows if "1. Purpose" in r[1])
    assert all(int(r[0]) == 2 for r in rows if "Payroll-GX" in r[1])


def test_six_asked_six_returned_under_the_type_filter(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = world[1]["acme"]
    on = retrieve(
        acme,
        EMBEDDER,
        "security policy vulnerability report timeline",
        evidence_type="policy_document",
        include_distractors=True,
        config=CONFIG,
    )
    assert len(on.chunks) == 6 and len(on.candidates) == CONFIG.candidates
    assert all(c.fused_rank == i + 1 for i, c in enumerate(on.candidates))
    off = retrieve(
        acme,
        EMBEDDER,
        "security policy vulnerability report timeline",
        evidence_type="policy_document",
        include_distractors=True,
        config=CONFIG,
        iterative_scan=False,
    )
    assert len(off.candidates) <= len(on.candidates)
    total = acme.fetch_one(
        "SELECT count(*) FROM chunks WHERE evidence_type = 'policy_document' AND NOT is_distractor"
    )
    plain = retrieve(
        acme, EMBEDDER, "security policy", evidence_type="policy_document", config=CONFIG
    )
    assert len(plain.chunks) == min(6, int(total[0]))  # type: ignore[index]
    assert not any(c.is_distractor for c in plain.candidates)


def test_the_relevant_chunk_ranks_first_for_the_disclosure_query(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = world[1]["acme"]
    result = retrieve(
        acme,
        EMBEDDER,
        "vulnerability disclosure response timeline acknowledge report business days",
        evidence_type="policy_document",
        subject_entity_id="github:asset:R_kgDOAcmeP001",
        reranker=LexicalOverlapReranker(),
        config=CONFIG,
    )
    assert result.chunks and "5 business days" in result.chunks[0].text
    top = result.chunks[0]
    assert top.rerank_rank == 1 and top.rerank_score is not None and top.fts_rank is not None
    assert all(c.subject_entity_id == "github:asset:R_kgDOAcmeP001" for c in result.candidates)
    scores = top.scores()
    assert {
        "fts_score",
        "fts_rank",
        "cos_score",
        "cos_rank",
        "fused_score",
        "fused_rank",
        "rerank_score",
        "rerank_rank",
    } <= set(scores)


def test_retrieval_refuses_to_mix_embedding_models(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    class Other(HashingEmbeddingClient):
        model_id = "voyage-3-large"

    with pytest.raises(EmbeddingModelMismatch, match="ccp index rebuild"):
        retrieve(world[1]["acme"], Other(), "anything", evidence_type="policy_document")


class _Failing(HashingEmbeddingClient):
    def embed(self, texts: Sequence[str], input_type: InputType) -> EmbeddingResult:
        if input_type == "query":
            raise RetryableModelError("provider down")
        return super().embed(texts, input_type)


class _FailingReranker(LexicalOverlapReranker):
    def rerank(self, query: str, documents: Sequence[str], top_k: int) -> object:  # type: ignore[override]
        raise RetryableModelError("reranker down")


def test_provider_outages_degrade_with_an_event(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = world[1]["acme"]
    lexical_only = retrieve(
        acme,
        _Failing(),
        "vulnerability disclosure timeline",
        evidence_type="policy_document",
        config=CONFIG,
    )
    assert lexical_only.degraded and any(
        e.startswith("embedding_unavailable") for e in lexical_only.events
    )
    assert lexical_only.chunks and all(c.cos_rank is None for c in lexical_only.candidates)
    fused = retrieve(
        acme,
        EMBEDDER,
        "vulnerability disclosure timeline",
        evidence_type="policy_document",
        reranker=_FailingReranker(),
        config=CONFIG,
    )  # type: ignore[arg-type]
    assert any(e.startswith("rerank_unavailable") for e in fused.events)
    assert fused.chunks == fused.candidates[:6] and fused.chunks[0].rerank_score is None


def test_rls_holds_on_chunks_without_scoped_db(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:  # S1 (b)
    settings, dbs = world
    acme_count = dbs["acme"].fetch_one("SELECT count(*) FROM chunks")
    role, password = app_role_and_password(settings)
    probe = vector_literal(EMBEDDER.embed(["security policy"], "query").vectors[0])
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        assert conn.execute("SELECT current_user").fetchone() == (role,)
        conn.execute("SELECT set_config('app.tenant_id', 'acme', false)")
        assert conn.execute("SELECT count(*) FROM chunks").fetchone() == acme_count
        rows = conn.execute(
            "SELECT tenant_id FROM chunks ORDER BY embedding <=> %s::vector LIMIT 5", (probe,)
        ).fetchall()
        assert rows and all(r[0] == "acme" for r in rows)
        conn.execute("SELECT set_config('app.tenant_id', '', false)")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("SELECT count(*) FROM chunks").fetchone()
    assert password  # the pool's credentials are the app role's, never the admin's


def test_distractors_are_deterministic_and_never_retrieved_by_default(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = world[1]["acme"]
    generated = distractors.generate("acme", 300)
    assert generated == distractors.generate("acme", 300) and len(generated) == 300
    assert {d.evidence_type for d in generated} == {
        "access_review_report",
        "training_record",
        "policy_document",
    }
    assert generated != distractors.generate("globex", 300)
    present = index.stats(acme)["distractors"]
    assert isinstance(present, int) and present >= 300
    assert distractors.install(acme, EMBEDDER, 300) == present  # idempotent
    result = retrieve(
        acme,
        EMBEDDER,
        "user access review scope",
        evidence_type="access_review_report",
        config=CONFIG,
    )
    assert not any(c.is_distractor for c in result.candidates)


def test_golden_queries_score_well_against_the_distractor_corpus(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = world[1]["acme"]
    queries = metrics.load_queries()
    assert len(queries) >= 30
    report = metrics.evaluate_queries(
        acme,
        EMBEDDER,
        queries,
        reranker=LexicalOverlapReranker(),
        config=CONFIG,
        include_distractors=True,
    )
    assert report.unresolved == [], report.unresolved
    assert len(report.queries) == sum(1 for q in queries if q.tenant == "acme")
    assert report.recall_at_5 >= 0.75 and report.mrr >= 0.6, report.summary()
    ablation = metrics.evaluate_queries(
        acme,
        EMBEDDER,
        queries,
        reranker=None,
        config=CONFIG,
        include_distractors=True,
        iterative_scan=False,
    )
    assert ablation.summary()["rerank_model"] is None and ablation.recall_at_5 >= 0.5


def test_calibration_records_the_noise_floor_per_model_pair(
    world: tuple[Settings, dict[str, ScopedDB]],
) -> None:
    acme = world[1]["acme"]
    queries = [q for q in metrics.load_queries() if q.tenant == "acme"]
    cal = calibration.calibrate(acme, EMBEDDER, queries, reranker=None, config=CONFIG)
    assert (
        cal.samples > 0
        and cal.key == "local-hashing-v1|none"
        and cal.thresholds.score_kind == "fused"
    )
    values = [cal.thresholds.percentiles[p] for p in (50, 75, 90, 95, 99)]
    assert values == sorted(values)
    updated = calibration.record(CONFIG, cal)
    assert updated.thresholds_for("local-hashing-v1", None) == cal.thresholds
    assert CONFIG.thresholds == {}  # the input config is untouched
