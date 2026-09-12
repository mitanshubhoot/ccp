"""Retrieval metrics over the golden query set (spec.md Layer 11): Recall@k, MRR, nDCG@k.

`evals/golden/retrieval.yaml` names relevant chunks by evidence id (or prefix) plus a
substring, because chunk ids are content hashes that nobody should type by hand; the ids
are resolved against the index at run time.
"""

from __future__ import annotations

import math
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
        }


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
        )
        ranked = [c.chunk_id for c in result.chunks] or [c.chunk_id for c in result.candidates]
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
            )
        )
    return report
