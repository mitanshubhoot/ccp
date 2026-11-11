"""Hybrid retrieval in one SQL statement (spec.md Layer 7 tier 2, A-46).

    query ──► embed once ──► ┌ fts CTE:   ts_rank_cd over an OR tsquery, top N   ┐
                             │ dense CTE: cosine over vector(1024), top N, with     │ RRF (k=60)
                             │   SET LOCAL hnsw.iterative_scan = relaxed_order and │ FULL OUTER JOIN
                             └   hnsw.ef_search per query (short-result fix)        ┘
          ──► top-20 candidates ──► rerank-2.5 (flag) ──► top-6

RLS scopes both arms; the query has no tenant predicate of its own. Distractor chunks are
excluded unless asked for (evaluation and calibration). A provider outage degrades:
no embedding -> lexical arm only; no reranker -> fused order; both recorded as events so the
confidence cap can see them. Retrieval refuses to mix embedding models.

As of a time, or against a named generation (A-145), the same query runs over the versioned
chunks instead of the serving copy: each document's newest chunk set recorded by then, in
the generation active then (or the one named). That is what the index held when a verdict
was made, and how a candidate generation is measured before it serves. The versioned arm
scans exactly (no HNSW), which is right for replay and gating volumes; distractors are never
versioned.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ccp.llm.errors import RetryableModelError, TerminalModelError
from ccp.retrieval import generations
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import DIMENSION, EmbeddingClient, vector_literal
from ccp.retrieval.rerank import Reranker
from ccp.storage.scoped import ScopedDB

_TOKEN = re.compile(r"[a-z0-9]+")


class EmbeddingModelMismatch(RuntimeError):
    """Chunks were embedded with another model: `ccp index reembed` gates a switch, and
    `ccp index rebuild` forces one."""


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    evidence_entity_id: str
    evidence_type: str
    subject_entity_id: str | None
    source_ref: str
    page: int | None
    text: str
    context: str | None
    fts_score: float | None
    fts_rank: int | None
    cos_score: float | None
    cos_rank: int | None
    fused_score: float
    fused_rank: int
    rerank_score: float | None = None
    rerank_rank: int | None = None
    is_distractor: bool = False

    @property
    def top_score(self) -> float:
        return self.rerank_score if self.rerank_score is not None else self.fused_score

    @property
    def text_for_rerank(self) -> str:
        return f"{self.context}\n{self.text}" if self.context else self.text

    def scores(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "fts_score": self.fts_score,
            "fts_rank": self.fts_rank,
            "cos_score": self.cos_score,
            "cos_rank": self.cos_rank,
            "fused_score": round(self.fused_score, 6),
            "fused_rank": self.fused_rank,
            "rerank_score": self.rerank_score,
            "rerank_rank": self.rerank_rank,
        }


@dataclass
class RetrievalResult:
    query: str
    embed_model: str
    rerank_model: str | None
    candidates: list[RetrievedChunk] = field(default_factory=list)  # fused order, top N
    chunks: list[RetrievedChunk] = field(default_factory=list)  # final order, top k
    events: list[str] = field(default_factory=list)
    generation_id: str | None = None  # set when the versioned chunks were searched
    as_of: datetime | None = None

    @property
    def degraded(self) -> bool:
        return any("_unavailable" in e for e in self.events)


def or_tsquery(query: str) -> str:
    tokens: list[str] = []
    for token in _TOKEN.findall(query.lower()):
        if token not in tokens:
            tokens.append(token)
    return " | ".join(tokens)


def _check_model(db: ScopedDB, embed_model: str, include_distractors: bool) -> None:
    rows = db.fetch_all(
        "SELECT DISTINCT embed_model FROM chunks WHERE (%s OR NOT is_distractor)",
        (include_distractors,),
    )
    others = sorted(str(r[0]) for r in rows if str(r[0]) != embed_model)
    if others:
        raise EmbeddingModelMismatch(
            f"chunks embedded with {others} but the configured model is {embed_model!r};"
            " run `ccp index reembed` (gated switch) or `ccp index rebuild` (forced)"
        )


_BODY = """
fts AS (
  SELECT chunk_id, ts_rank_cd(tsv, to_tsquery('english', %(tsq)s)) AS score
  FROM {src}
  WHERE %(use_fts)s
    AND evidence_type = %(etype)s
    AND (%(subject)s::text IS NULL OR subject_entity_id = %(subject)s)
    AND (%(distractors)s OR NOT is_distractor)
    AND tsv @@ to_tsquery('english', %(tsq)s)
  ORDER BY score DESC, chunk_id
  LIMIT %(n)s
), fts_ranked AS (
  SELECT chunk_id, score, row_number() OVER (ORDER BY score DESC, chunk_id) AS rank FROM fts
), dense AS (
  SELECT chunk_id, embedding <=> %(vec)s::vector AS dist
  FROM {src}
  WHERE %(use_dense)s
    AND evidence_type = %(etype)s
    AND (%(subject)s::text IS NULL OR subject_entity_id = %(subject)s)
    AND (%(distractors)s OR NOT is_distractor)
    AND embedding IS NOT NULL
  ORDER BY embedding <=> %(vec)s::vector
  LIMIT %(n)s
), dense_ranked AS (
  SELECT chunk_id, 1 - dist AS score, row_number() OVER (ORDER BY dist, chunk_id) AS rank
  FROM dense
), fused AS (
  SELECT COALESCE(f.chunk_id, d.chunk_id) AS chunk_id,
         f.score AS fts_score, f.rank AS fts_rank, d.score AS cos_score, d.rank AS cos_rank,
         COALESCE(1.0 / (%(rrf_k)s + f.rank), 0) + COALESCE(1.0 / (%(rrf_k)s + d.rank), 0)
           AS fused_score
  FROM fts_ranked f FULL OUTER JOIN dense_ranked d ON f.chunk_id = d.chunk_id
)
SELECT c.chunk_id, c.evidence_entity_id, c.evidence_type, c.subject_entity_id, c.source_ref,
       c.page, c.text, c.context, fu.fts_score, fu.fts_rank, fu.cos_score, fu.cos_rank,
       fu.fused_score, c.is_distractor
FROM fused fu JOIN {src} c ON c.chunk_id = fu.chunk_id
ORDER BY fu.fused_score DESC, c.chunk_id
LIMIT %(n)s
"""
_SQL = "WITH" + _BODY.format(src="chunks")
_SQL_VERSIONED = f"WITH versioned AS MATERIALIZED {generations.AS_OF_SOURCE}," + _BODY.format(
    src="versioned"
)


def retrieve(
    db: ScopedDB,
    embedder: EmbeddingClient,
    query: str,
    *,
    evidence_type: str,
    subject_entity_id: str | None = None,
    reranker: Reranker | None = None,
    config: RetrievalConfig | None = None,
    include_distractors: bool = False,
    iterative_scan: bool = True,
    top_k: int | None = None,
    as_of: datetime | None = None,
    generation_id: str | None = None,
) -> RetrievalResult:
    cfg = config or RetrievalConfig()
    k = top_k or cfg.top_k
    result = RetrievalResult(
        query=query,
        embed_model=embedder.model_id,
        rerank_model=reranker.model_id if reranker else None,
        as_of=as_of,
    )
    versioned = as_of is not None or generation_id is not None
    if versioned:
        found = (
            generations.describe(db, generation_id)
            if generation_id is not None
            else generations.active(db, as_of)
        )
        if found is None:
            result.events.append("no_generation_as_of")
            return result
        if found.embed_model != embedder.model_id:
            raise EmbeddingModelMismatch(
                f"generation {found.generation_id} was embedded with {found.embed_model!r},"
                f" not {embedder.model_id!r}"
            )
        result.generation_id = found.generation_id
    else:
        _check_model(db, embedder.model_id, include_distractors)

    vector: Sequence[float] | None = None
    try:
        vector = embedder.embed([query], "query").vectors[0]
    except (RetryableModelError, TerminalModelError) as exc:
        result.events.append(f"embedding_unavailable:{type(exc).__name__}")
    tsq = or_tsquery(query)
    if not tsq:
        result.events.append("empty_lexical_query")

    params: dict[str, Any] = {
        "tsq": tsq or "x",
        "use_fts": bool(tsq),
        "etype": evidence_type,
        "subject": subject_entity_id,
        "distractors": include_distractors,
        "n": cfg.candidates,
        "vec": vector_literal(vector if vector is not None else [0.0] * DIMENSION),
        "use_dense": vector is not None,
        "rrf_k": cfg.rrf_k,
        "gen": result.generation_id,
        "as_of": as_of,
    }
    with db.transaction() as cur:
        cur.execute(
            "SELECT set_config('hnsw.iterative_scan', %s, true)",
            ("relaxed_order" if iterative_scan else "off",),
        )
        cur.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(cfg.ef_search),))
        rows = cur.execute(_SQL_VERSIONED if versioned else _SQL, params).fetchall()
    candidates = [
        RetrievedChunk(
            chunk_id=str(r[0]),
            evidence_entity_id=str(r[1]),
            evidence_type=str(r[2]),
            subject_entity_id=str(r[3]) if r[3] is not None else None,
            source_ref=str(r[4]),
            page=int(r[5]) if r[5] is not None else None,
            text=str(r[6]),
            context=str(r[7]) if r[7] is not None else None,
            fts_score=float(r[8]) if r[8] is not None else None,
            fts_rank=int(r[9]) if r[9] is not None else None,
            cos_score=float(r[10]) if r[10] is not None else None,
            cos_rank=int(r[11]) if r[11] is not None else None,
            fused_score=float(r[12]),
            fused_rank=i + 1,
            is_distractor=bool(r[13]),
        )
        for i, r in enumerate(rows)
    ]
    result.candidates = candidates
    if not candidates:
        return result

    if reranker is None:
        result.chunks = candidates[:k]
        return result
    try:
        reranked = reranker.rerank(query, [c.text_for_rerank for c in candidates], k)
    except (RetryableModelError, TerminalModelError) as exc:
        result.events.append(f"rerank_unavailable:{type(exc).__name__}")
        result.chunks = candidates[:k]
        return result
    ordered: list[RetrievedChunk] = []
    for position, hit in enumerate(reranked.hits[:k], start=1):
        base = candidates[hit.index]
        ordered.append(
            RetrievedChunk(**{**base.__dict__, "rerank_score": hit.score, "rerank_rank": position})
        )
    result.chunks = ordered
    return result
