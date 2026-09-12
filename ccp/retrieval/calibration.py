"""Percentile calibration (spec.md D25): the score distribution of the best-scoring distractor
per golden query is the noise floor for this (embedding model, reranker) pair. Stored in
config/retrieval.yaml with the measurement date; the confidence cap reads it."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ccp.retrieval import config as retrieval_config
from ccp.retrieval.config import PERCENTILES, RetrievalConfig, Thresholds
from ccp.retrieval.embeddings import EmbeddingClient
from ccp.retrieval.hybrid import retrieve
from ccp.retrieval.metrics import RetrievalQuery
from ccp.retrieval.rerank import Reranker
from ccp.storage.scoped import ScopedDB


@dataclass
class Calibration:
    key: str
    thresholds: Thresholds
    samples: int


def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
    return round(ordered[index], 6)


def calibrate(
    db: ScopedDB,
    embedder: EmbeddingClient,
    queries: list[RetrievalQuery],
    *,
    reranker: Reranker | None,
    config: RetrievalConfig,
) -> Calibration:
    """Best distractor score per query, over the candidate list (rerank score when a reranker
    is on, fused score otherwise)."""
    scores: list[float] = []
    for query in queries:
        if query.tenant != db.tenant.tenant_id:
            continue
        result = retrieve(
            db,
            embedder,
            query.query,
            evidence_type=query.evidence_type,
            subject_entity_id=None,  # distractors carry their own subjects; widen the scope
            reranker=reranker,
            config=config,
            include_distractors=True,
            top_k=config.candidates,
        )
        pool = result.chunks if reranker else result.candidates
        best = max((c.top_score for c in pool if c.is_distractor), default=None)
        if best is not None:
            scores.append(best)
    if not scores:
        raise ValueError(
            "no distractor chunks scored; install distractors (ccp index distractors) first"
        )
    thresholds = Thresholds(
        measured_at=retrieval_config.now_stamp(),
        queries=len(scores),
        percentiles={p: percentile(scores, p) for p in PERCENTILES},
        score_kind="rerank" if reranker else "fused",
    )
    key = RetrievalConfig.key(embedder.model_id, reranker.model_id if reranker else None)
    return Calibration(key=key, thresholds=thresholds, samples=len(scores))


def record(config: RetrievalConfig, calibration: Calibration) -> RetrievalConfig:
    updated = config.model_copy(deep=True)
    updated.thresholds[calibration.key] = calibration.thresholds
    return updated
