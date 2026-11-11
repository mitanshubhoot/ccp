"""Percentile calibration (spec.md D25): the score distribution of the best-scoring distractor
per golden query is the noise floor for this (embedding model, reranker) pair. Stored in
config/retrieval.yaml with the measurement date; the confidence cap reads it.

Reliability (A-148): from the same golden run, (top score, relevant chunk in the top k)
pairs are fitted by isotonic regression (pool-adjacent-violators), smoothed (hits + 1) /
(n + 2), into a non-decreasing curve: per evidence type where it has enough queries, and
pooled for the pair. Confidence then reads a measured hit rate, not a flat 0.60."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ccp.retrieval import config as retrieval_config
from ccp.retrieval.config import (
    MIN_RELIABILITY_SAMPLES,
    PERCENTILES,
    Reliability,
    ReliabilityBin,
    RetrievalConfig,
    Thresholds,
)
from ccp.retrieval.embeddings import EmbeddingClient
from ccp.retrieval.hybrid import retrieve
from ccp.retrieval.metrics import MetricsReport, RetrievalQuery
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


def fit_reliability(pairs: list[tuple[float, bool]], score_kind: str) -> Reliability:
    """Isotonic (non-decreasing) hit rate over top score, smoothed, as bins."""
    blocks: list[list[float]] = []  # [max_score, hits, n]
    for score, hit in sorted(pairs):
        if blocks and blocks[-1][0] == score:  # a tie is one point, never two bins
            blocks[-1][1] += 1.0 if hit else 0.0
            blocks[-1][2] += 1.0
        else:
            blocks.append([score, 1.0 if hit else 0.0, 1.0])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][2] > blocks[-1][1] / blocks[-1][2]:
            last = blocks.pop()
            blocks[-1] = [last[0], blocks[-1][1] + last[1], blocks[-1][2] + last[2]]
    bins: list[ReliabilityBin] = []
    running = 0.0
    for top, hits, n in blocks:
        running = max(running, (hits + 1) / (n + 2))  # smoothing must not undo the order
        bins.append(ReliabilityBin(max_score=round(top, 6), p=round(running, 4), n=int(n)))
    return Reliability(
        measured_at=retrieval_config.now_stamp(),
        samples=len(pairs),
        score_kind=score_kind,
        bins=bins,
    )


def reliability_curves(report: MetricsReport) -> dict[str, Reliability]:
    """The pooled curve for the pair, and one per evidence type with enough queries."""
    kind = "rerank" if report.rerank_model else "fused"
    pairs = {
        q.evidence_type: [
            (float(x.top_score), x.hit_at_k)
            for x in report.queries
            if x.evidence_type == q.evidence_type and x.top_score is not None
        ]
        for q in report.queries
    }
    pooled = [(float(q.top_score), q.hit_at_k) for q in report.queries if q.top_score is not None]
    key = RetrievalConfig.reliability_key
    curves: dict[str, Reliability] = {}
    if len(pooled) >= MIN_RELIABILITY_SAMPLES:
        curves[key(report.embed_model, report.rerank_model, "*")] = fit_reliability(pooled, kind)
    for etype, found in pairs.items():
        if len(found) >= MIN_RELIABILITY_SAMPLES:
            curves[key(report.embed_model, report.rerank_model, etype)] = fit_reliability(
                found, kind
            )
    return curves


def record_reliability(config: RetrievalConfig, curves: dict[str, Reliability]) -> RetrievalConfig:
    updated = config.model_copy(deep=True)
    updated.reliability.update(curves)
    return updated
