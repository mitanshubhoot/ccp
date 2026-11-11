"""Retrieval gates per evidence type (spec.md A-148).

One overall Recall@5 hides a type that retrieves badly: a few strong policy queries can carry
a training-record set that never finds its document. Each evidence type in the golden set
is gated on its own, from config/retrieval_gates.yaml:

    recall_at_5_min        mean Recall@5 over the type's queries
    mrr_min                mean reciprocal rank of the first relevant chunk
    distractor_at_1_max    share of queries whose top hit is a distractor (with distractors in)
    min_queries            below this the type is reported as ungated, never as passing

A golden query whose relevant chunks no longer resolve blocks too: the set has drifted from
the corpus, and a metric over it would mean nothing. `ccp index gate` runs them; a parser,
chunker, embedding model or reranker change ships only when they hold (Decision 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ccp.config import REPO_ROOT
from ccp.retrieval.metrics import MetricsReport, TypeMetrics

GATES_FILE = REPO_ROOT / "config" / "retrieval_gates.yaml"


class TypeGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_queries: int = Field(default=3, ge=1)
    recall_at_5_min: float = Field(default=0.6, ge=0, le=1)
    mrr_min: float = Field(default=0.5, ge=0, le=1)
    distractor_at_1_max: float = Field(default=0.1, ge=0, le=1)


class RetrievalGates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: TypeGate = Field(default_factory=TypeGate)
    types: dict[str, dict[str, float]] = Field(default_factory=dict)
    # embedding model -> evidence type or "*" -> overrides (the keyless stand-in's floors)
    models: dict[str, dict[str, dict[str, float]]] = Field(default_factory=dict)

    def for_type(self, evidence_type: str, embed_model: str | None = None) -> TypeGate:
        merged: dict[str, float] = {**self.default.model_dump()}
        merged.update(self.types.get(evidence_type, {}))
        per_model = self.models.get(embed_model or "", {})
        merged.update(per_model.get("*", {}))
        merged.update(per_model.get(evidence_type, {}))
        return TypeGate.model_validate(merged)


def load(path: Path = GATES_FILE) -> RetrievalGates:
    if not path.is_file():
        return RetrievalGates()
    return RetrievalGates.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


@dataclass(frozen=True)
class GateOutcome:
    evidence_type: str
    metrics: TypeMetrics
    gated: bool
    breaches: tuple[str, ...]


def check(report: MetricsReport, gates: RetrievalGates | None = None) -> list[GateOutcome]:
    rules = gates or load()
    out: list[GateOutcome] = []
    for etype, m in report.by_type().items():
        gate = rules.for_type(etype, report.embed_model)
        if m.queries < gate.min_queries:
            out.append(GateOutcome(etype, m, False, ()))
            continue
        found: list[str] = []
        if m.recall_at_5 < gate.recall_at_5_min:
            found.append(f"recall_at_5 {m.recall_at_5:.3f} < {gate.recall_at_5_min}")
        if m.mrr < gate.mrr_min:
            found.append(f"mrr {m.mrr:.3f} < {gate.mrr_min}")
        if report.include_distractors and m.distractor_at_1 > gate.distractor_at_1_max:
            found.append(f"distractor_at_1 {m.distractor_at_1:.3f} > {gate.distractor_at_1_max}")
        out.append(GateOutcome(etype, m, True, tuple(found)))
    return out


def blocking(report: MetricsReport, outcomes: list[GateOutcome]) -> list[str]:
    """Every reason the report does not pass: per-type breaches and unresolved queries."""
    reasons = [f"{o.evidence_type}: {b}" for o in outcomes for b in o.breaches]
    if report.unresolved:
        reasons.append(f"unresolved golden queries: {sorted(report.unresolved)}")
    return reasons
