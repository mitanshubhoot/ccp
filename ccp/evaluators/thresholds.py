"""config/evaluation.yaml: the human-interrupt threshold (spec.md A-10, A-47)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ccp.config import REPO_ROOT

CONFIG_PATH = REPO_ROOT / "config" / "evaluation.yaml"


class EvaluationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interrupt_threshold: float = Field(default=0.70, ge=0, le=1)
    read_from_curve_on: str | None = None


@lru_cache(maxsize=4)
def load(path: Path = CONFIG_PATH) -> EvaluationConfig:
    if not path.is_file():
        return EvaluationConfig()
    return EvaluationConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def interrupt_threshold() -> float:
    return load().interrupt_threshold
