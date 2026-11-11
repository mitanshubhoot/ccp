"""Paired comparison of two retrieval configurations on the golden queries (spec.md A-149).

A new embedding model or reranker (voyage-4-large, rerank-3) is judged against the serving
pair on the same queries, query by query, so the noise between queries cancels:

    baseline   the serving generation, its model embedding the queries, its reranker
    candidate  the candidate's generation (written beside the serving one, never switched
               here), its model, its reranker; either side may be a reranker change only

For each query: reciprocal rank and Recall@5 on both sides, and the retrieval latency. The
summary gives the mean paired difference with a 95% bootstrap interval (resampled queries,
a fixed seed so a rerun gives the same interval), an exact two-sided sign test over the
queries that changed, per evidence type deltas, and the latency medians. Its verdict is
"candidate_better" only when the interval for the MRR difference lies above zero, and
"candidate_worse" when it lies below; anything else is "no_detectable_difference".

The comparison informs; it does not switch. Serving a new model is `ccp index reembed`,
which applies the gate (A-145, A-148) and records the activation.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ccp.retrieval import metrics
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import EmbeddingClient
from ccp.retrieval.rerank import Reranker
from ccp.storage.scoped import ScopedDB

BOOTSTRAP_ITERATIONS = 2000
SEED = 7


@dataclass(frozen=True)
class Side:
    embedder: EmbeddingClient
    reranker: Reranker | None
    generation_id: str

    @property
    def name(self) -> str:
        rerank = self.reranker.model_id if self.reranker else "none"
        return f"{self.embedder.model_id}|{rerank}"


@dataclass(frozen=True)
class Pair:
    id: str
    evidence_type: str
    baseline_rr: float
    candidate_rr: float
    baseline_recall: float
    candidate_recall: float
    baseline_ms: float
    candidate_ms: float


def bootstrap_ci(
    deltas: Sequence[float],
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = SEED,
    confidence: float = 0.95,
) -> tuple[float, float]:
    if not deltas:
        return (0.0, 0.0)
    rng = random.Random(seed)  # noqa: S311 - resampling, not secrecy
    n = len(deltas)
    means = sorted(sum(rng.choice(deltas) for _ in range(n)) / n for _ in range(iterations))
    tail = (1 - confidence) / 2
    lo = means[max(0, math.floor(tail * iterations))]
    hi = means[min(iterations - 1, math.ceil((1 - tail) * iterations) - 1)]
    return (round(lo, 4), round(hi, 4))


def sign_test(wins: int, losses: int) -> float:
    """Exact two-sided binomial p-value over the queries that changed (ties dropped)."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail: float = sum(math.comb(n, i) for i in range(k + 1)) / float(2**n)
    return round(min(1.0, 2 * tail), 4)


@dataclass
class Comparison:
    baseline: str
    candidate: str
    pairs: list[Pair] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        rr = [p.candidate_rr - p.baseline_rr for p in self.pairs]
        recall = [p.candidate_recall - p.baseline_recall for p in self.pairs]
        wins = sum(1 for d in rr if d > 0)
        losses = sum(1 for d in rr if d < 0)
        mrr_ci = bootstrap_ci(rr)
        if mrr_ci[0] > 0:
            verdict = "candidate_better"
        elif mrr_ci[1] < 0:
            verdict = "candidate_worse"
        else:
            verdict = "no_detectable_difference"
        by_type: dict[str, dict[str, float]] = {}
        for etype in sorted({p.evidence_type for p in self.pairs}):
            mine = [p for p in self.pairs if p.evidence_type == etype]
            by_type[etype] = {
                "queries": len(mine),
                "mrr_delta": round(
                    statistics.fmean(p.candidate_rr - p.baseline_rr for p in mine), 4
                ),
                "recall_at_5_delta": round(
                    statistics.fmean(p.candidate_recall - p.baseline_recall for p in mine), 4
                ),
            }
        return {
            "baseline": self.baseline,
            "candidate": self.candidate,
            "queries": len(self.pairs),
            "unresolved": self.unresolved,
            "baseline_mrr": round(statistics.fmean(p.baseline_rr for p in self.pairs), 4)
            if self.pairs
            else 0.0,
            "candidate_mrr": round(statistics.fmean(p.candidate_rr for p in self.pairs), 4)
            if self.pairs
            else 0.0,
            "mrr_delta": round(statistics.fmean(rr), 4) if rr else 0.0,
            "mrr_delta_ci95": list(mrr_ci),
            "recall_at_5_delta": round(statistics.fmean(recall), 4) if recall else 0.0,
            "recall_at_5_delta_ci95": list(bootstrap_ci(recall)),
            "wins": wins,
            "losses": losses,
            "ties": len(rr) - wins - losses,
            "sign_test_p": sign_test(wins, losses),
            "latency_ms_p50": {
                "baseline": round(statistics.median(p.baseline_ms for p in self.pairs), 2)
                if self.pairs
                else 0.0,
                "candidate": round(statistics.median(p.candidate_ms for p in self.pairs), 2)
                if self.pairs
                else 0.0,
            },
            "by_type": by_type,
            "verdict": verdict,
        }


def compare(
    db: ScopedDB,
    queries: list[metrics.RetrievalQuery],
    baseline: Side,
    candidate: Side,
    config: RetrievalConfig | None = None,
) -> Comparison:
    """Both sides over the versioned chunks of their own generation, distractors out."""
    runs = [
        metrics.evaluate_queries(
            db,
            side.embedder,
            queries,
            reranker=side.reranker,
            config=config,
            include_distractors=False,
            generation_id=side.generation_id,
        )
        for side in (baseline, candidate)
    ]
    found = {q.id: q for q in runs[1].queries}
    out = Comparison(baseline.name, candidate.name, unresolved=sorted(set(runs[0].unresolved)))
    for b in runs[0].queries:
        c = found.get(b.id)
        if c is None:
            out.unresolved.append(b.id)
            continue
        out.pairs.append(
            Pair(
                b.id,
                b.evidence_type,
                b.reciprocal_rank,
                c.reciprocal_rank,
                b.recall_at_5,
                c.recall_at_5,
                b.latency_ms,
                c.latency_ms,
            )
        )
    return out


def merge(comparisons: list[Comparison]) -> Comparison:
    first = comparisons[0]
    return Comparison(
        first.baseline,
        first.candidate,
        [p for c in comparisons for p in c.pairs],
        [u for c in comparisons for u in c.unresolved],
    )
