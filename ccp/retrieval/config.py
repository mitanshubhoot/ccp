"""config/retrieval.yaml: the query knobs and the measured percentile thresholds
(spec.md A-46, D25). Thresholds are recorded per (embedding model, reranker) with the
measurement date; an unmeasured pair caps retrieval quality at 0.60 with an event."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ccp.config import REPO_ROOT

CONFIG_PATH = REPO_ROOT / "config" / "retrieval.yaml"
PERCENTILES = (50, 75, 90, 95, 99)


class Thresholds(BaseModel):
    """Score percentiles of the best-scoring *distractor* per golden query: the noise floor."""

    model_config = ConfigDict(extra="forbid")

    measured_at: str
    queries: int
    percentiles: dict[int, float]  # percentile -> score
    score_kind: str  # "rerank" or "fused"


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ef_search: int = Field(default=100, ge=1)
    candidates: int = Field(default=20, ge=1)
    top_k: int = Field(default=6, ge=1)
    rrf_k: int = Field(default=60, ge=1)
    thresholds: dict[str, Thresholds] = Field(default_factory=dict)  # "<embed>|<rerank>"

    @staticmethod
    def key(embed_model: str, rerank_model: str | None) -> str:
        return f"{embed_model}|{rerank_model or 'none'}"

    def thresholds_for(self, embed_model: str, rerank_model: str | None) -> Thresholds | None:
        return self.thresholds.get(self.key(embed_model, rerank_model))


def load(path: Path = CONFIG_PATH) -> RetrievalConfig:
    if not path.is_file():
        return RetrievalConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return RetrievalConfig.model_validate(raw)


def save(config: RetrievalConfig, path: Path = CONFIG_PATH) -> None:
    payload: dict[str, Any] = config.model_dump(mode="json")
    header = (
        "# Retrieval knobs and measured thresholds (ccp index calibrate writes the thresholds).\n"
        "# percentiles are scores of the best distractor per golden query: the noise floor a\n"
        "# real top hit must beat; retrieval quality is the fraction of that floor it exceeds.\n"
    )
    path.write_text(header + yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def quality_from_thresholds(score: float, thresholds: Thresholds | None) -> float | None:
    """Fraction of the noise distribution below `score`, interpolated; None when unmeasured."""
    if thresholds is None or not thresholds.percentiles:
        return None
    points = sorted((p / 100.0, s) for p, s in thresholds.percentiles.items())
    if score <= points[0][1]:
        return round(points[0][0] * (score / points[0][1]) if points[0][1] > 0 else 0.0, 4)
    for (p_lo, s_lo), (p_hi, s_hi) in zip(points, points[1:], strict=False):
        if s_lo <= score <= s_hi:
            if s_hi == s_lo:
                return round(p_hi, 4)
            return round(p_lo + (p_hi - p_lo) * (score - s_lo) / (s_hi - s_lo), 4)
    return 1.0


def now_stamp() -> str:
    return datetime.now(UTC).date().isoformat()
