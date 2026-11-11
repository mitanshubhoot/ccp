"""Retrieval metrics over the golden query set (spec.md Layer 11): Recall@k, MRR, nDCG@k.

Per evidence type too (A-148): each query also records whether a relevant chunk is in the
top k a tier receives (`hit_at_k`, what calibrated confidence is fitted on), whether a
distractor ranked first, and how many distractors reached the top 5; `by_type` aggregates
them for the per-type gates (ccp/retrieval/gates.py).

`evals/golden/retrieval.yaml` names relevant chunks by evidence id (or prefix) plus a
substring, because chunk ids are content hashes that nobody should type by hand; the ids
are resolved against the index at run time.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ccp.config import REPO_ROOT
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import EmbeddingClient
from ccp.retrieval.hybrid import RetrievalResult, retrieve
from ccp.retrieval.rerank import Reranker
from ccp.storage.scoped import ScopedDB

GOLDEN_RETRIEVAL = REPO_ROOT / "evals" / "golden" / "retrieval.yaml"


class Relevant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: str | None = None
    evidence_prefix: str | None = None
    contains: str


class RetrievalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tenant: str
    query: str
    evidence_type: str
    subject_entity_id: str | None = None
    relevant: list[Relevant] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class RetrievalGolden(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: list[RetrievalQuery]


def load_queries(path: Path = GOLDEN_RETRIEVAL) -> list[RetrievalQuery]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return RetrievalGolden.model_validate(raw).queries


def resolve_relevant(db: ScopedDB, query: RetrievalQuery) -> set[str]:
    ids: set[str] = set()
    for rel in query.relevant:
        if rel.evidence is not None:
            rows = db.fetch_all(
                "SELECT chunk_id FROM chunks WHERE evidence_entity_id = %s AND text ILIKE %s",
                (rel.evidence, f"%{rel.contains}%"),
            )
        else:
            rows = db.fetch_all(
                "SELECT chunk_id FROM chunks WHERE strpos(evidence_entity_id, %s) = 1"
                " AND text ILIKE %s",
                (rel.evidence_prefix or "", f"%{rel.contains}%"),
            )
        ids.update(str(r[0]) for r in rows)
    return ids


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    for i, cid in enumerate(ranked, start=1):
        if cid in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(
        1.0 / math.log2(i + 1) for i, cid in enumerate(ranked[:k], start=1) if cid in relevant
    )
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


@dataclass
class QueryOutcome:
    id: str
    recall_at_5: float
    reciprocal_rank: float
    ndcg_at_10: float
    relevant: int
    retrieved: int
    top_score: float | None
    events: list[str] = field(default_factory=list)
    evidence_type: str = ""
    latency_ms: float = 0.0  # the retrieval call, query embedding and rerank included
    hit_at_k: bool = False  # a relevant chunk among the top_k a tier is given
    distractor_at_1: bool = False
    distractors_at_5: int = 0


@dataclass(frozen=True)
class TypeMetrics:
    queries: int
    recall_at_5: float
    mrr: float
    distractor_at_1: float  # share of queries whose top hit was a distractor
    distractor_share_at_5: float


@dataclass
class MetricsReport:
    embed_model: str
    rerank_model: str | None
    include_distractors: bool
    iterative_scan: bool
    queries: list[QueryOutcome] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def recall_at_5(self) -> float:
        return _mean([q.recall_at_5 for q in self.queries])

    @property
    def mrr(self) -> float:
        return _mean([q.reciprocal_rank for q in self.queries])

    @property
    def ndcg_at_10(self) -> float:
        return _mean([q.ndcg_at_10 for q in self.queries])

    @property
    def distractor_at_1(self) -> float:
        return _mean([1.0 if q.distractor_at_1 else 0.0 for q in self.queries])

    def by_type(self) -> dict[str, TypeMetrics]:
        grouped: dict[str, list[QueryOutcome]] = {}
        for q in self.queries:
            grouped.setdefault(q.evidence_type, []).append(q)
        return {
            etype: TypeMetrics(
                queries=len(qs),
                recall_at_5=round(_mean([q.recall_at_5 for q in qs]), 4),
                mrr=round(_mean([q.reciprocal_rank for q in qs]), 4),
                distractor_at_1=round(_mean([1.0 if q.distractor_at_1 else 0.0 for q in qs]), 4),
                distractor_share_at_5=round(_mean([q.distractors_at_5 / 5 for q in qs]), 4),
            )
            for etype, qs in sorted(grouped.items())
        }

    def summary(self) -> dict[str, Any]:
        return {
            "embed_model": self.embed_model,
            "rerank_model": self.rerank_model,
            "include_distractors": self.include_distractors,
            "iterative_scan": self.iterative_scan,
            "queries": len(self.queries),
            "unresolved": self.unresolved,
            "recall_at_5": round(self.recall_at_5, 4),
            "mrr": round(self.mrr, 4),
            "ndcg_at_10": round(self.ndcg_at_10, 4),
            "distractor_at_1": round(self.distractor_at_1, 4),
            "by_type": {t: m.__dict__ for t, m in self.by_type().items()},
        }


def merge(reports: list[MetricsReport]) -> MetricsReport:
    """Several tenants' runs of one configuration as one report, so each evidence type is
    gated over every golden query it has."""
    if not reports:
        raise ValueError("nothing to merge")
    first = reports[0]
    if any(
        (r.embed_model, r.rerank_model) != (first.embed_model, first.rerank_model) for r in reports
    ):
        raise ValueError("reports of different (embedding model, reranker) pairs")
    return MetricsReport(
        embed_model=first.embed_model,
        rerank_model=first.rerank_model,
        include_distractors=all(r.include_distractors for r in reports),
        iterative_scan=first.iterative_scan,
        queries=[q for r in reports for q in r.queries],
        unresolved=[u for r in reports for u in r.unresolved],
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def evaluate_queries(
    db: ScopedDB,
    embedder: EmbeddingClient,
    queries: list[RetrievalQuery],
    *,
    reranker: Reranker | None = None,
    config: RetrievalConfig | None = None,
    include_distractors: bool = True,
    iterative_scan: bool = True,
    generation_id: str | None = None,
) -> MetricsReport:
    report = MetricsReport(
        embed_model=embedder.model_id,
        rerank_model=reranker.model_id if reranker else None,
        include_distractors=include_distractors,
        iterative_scan=iterative_scan,
    )
    for query in queries:
        if query.tenant != db.tenant.tenant_id:
            continue
        relevant = resolve_relevant(db, query)
        if not relevant:
            report.unresolved.append(query.id)
            continue
        started = time.perf_counter()
        result: RetrievalResult = retrieve(
            db,
            embedder,
            query.query,
            evidence_type=query.evidence_type,
            subject_entity_id=query.subject_entity_id,
            reranker=reranker,
            config=config,
            include_distractors=include_distractors,
            iterative_scan=iterative_scan,
            top_k=10,
            generation_id=generation_id,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        ranked = [c.chunk_id for c in result.chunks] or [c.chunk_id for c in result.candidates]
        listed = result.chunks or result.candidates
        given = (config or RetrievalConfig()).top_k
        report.queries.append(
            QueryOutcome(
                id=query.id,
                recall_at_5=recall_at_k(ranked, relevant, 5),
                reciprocal_rank=reciprocal_rank(ranked, relevant),
                ndcg_at_10=ndcg_at_k(ranked, relevant, 10),
                relevant=len(relevant),
                retrieved=len(ranked),
                top_score=result.chunks[0].top_score if result.chunks else None,
                events=list(result.events),
                evidence_type=query.evidence_type,
                latency_ms=round(latency_ms, 2),
                hit_at_k=bool(set(ranked[:given]) & relevant),
                distractor_at_1=bool(listed) and listed[0].is_distractor,
                distractors_at_5=sum(1 for c in listed[:5] if c.is_distractor),
            )
        )
    return report
