"""Re-embedding into a new index generation, switched only after the retrieval gate (A-145).

    reembed(db, settings, candidate, serving_embedder)
        1. refresh with the candidate model: its generation's chunk sets are written beside
           the serving ones; `chunks` is not touched
        2. measure both generations on the tenant's retrieval golden queries (Recall@5, MRR),
           each with its own model embedding the queries
        3. gate: the candidate resolves queries and loses no more than TOLERANCE on either
           metric; then record the activation (chained, with the measurements) and rebuild
           `chunks` from the candidate's versions

A failed gate leaves the serving generation in place; `--force` switches anyway and records
'forced' with the failing measurements. The old generation's rows stay, so a verdict made
under it can still be replayed against exactly what it searched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ccp.config import Settings
from ccp.llm.client import LLMClient
from ccp.retrieval import generations, index, metrics
from ccp.retrieval.embeddings import EmbeddingClient
from ccp.storage.scoped import ScopedDB

TOLERANCE = 0.02


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: list[str]
    serving: dict[str, Any] | None
    candidate: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": self.reasons,
            "serving": self.serving,
            "candidate": self.candidate,
            "tolerance": TOLERANCE,
        }


def gate(
    serving: metrics.MetricsReport | None,
    candidate: metrics.MetricsReport,
    tolerance: float = TOLERANCE,
) -> GateResult:
    reasons: list[str] = []
    if not candidate.queries:
        reasons.append("no retrieval golden queries resolved for this tenant")
    if serving is not None and serving.queries:
        for name in ("recall_at_5", "mrr"):
            now, then = getattr(candidate, name), getattr(serving, name)
            if now < then - tolerance:
                reasons.append(f"{name} {now:.3f} < serving {then:.3f} - {tolerance}")
    return GateResult(
        passed=not reasons,
        reasons=reasons,
        serving=serving.summary() if serving is not None else None,
        candidate=candidate.summary(),
    )


@dataclass
class ReembedResult:
    candidate_generation: str
    serving_generation: str | None
    refresh: index.IndexSummary
    gate: GateResult | None = None
    switched: bool = False
    projected: int = 0
    events: list[str] = field(default_factory=list)


def reembed(
    db: ScopedDB,
    settings: Settings,
    candidate: EmbeddingClient,
    serving_embedder: EmbeddingClient | None,
    *,
    llm: LLMClient | None = None,
    queries: list[metrics.RetrievalQuery] | None = None,
    switch: bool = True,
    force: bool = False,
) -> ReembedResult:
    before = generations.active(db)
    summary = index.refresh(db, settings, candidate, llm)
    result = ReembedResult(
        candidate_generation=summary.generation_id,
        serving_generation=before.generation_id if before else None,
        refresh=summary,
    )
    if before is None or before.generation_id == summary.generation_id:
        result.events.append("already_serving")
        result.switched = before is None  # the first index serves without a gate
        return result
    asked = queries if queries is not None else metrics.load_queries()
    measured = metrics.evaluate_queries(
        db, candidate, asked, include_distractors=False, generation_id=summary.generation_id
    )
    baseline = (
        metrics.evaluate_queries(
            db,
            serving_embedder,
            asked,
            include_distractors=False,
            generation_id=before.generation_id,
        )
        if serving_embedder is not None
        else None
    )
    if baseline is None:
        result.events.append("serving_unmeasured:no_embedder_for_its_model")
    result.gate = gate(baseline, measured)
    if not switch or not (result.gate.passed or force):
        return result
    record = result.gate.as_json() | {"replaced": before.generation_id}
    generations.activate(
        db, summary.generation_id, "gate_passed" if result.gate.passed else "forced", record
    )
    result.projected = generations.project(db, summary.generation_id)
    result.switched = True
    return result
