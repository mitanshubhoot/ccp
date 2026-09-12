"""Weights and thresholds from config/identity.yaml, recorded on every decision."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from ccp.config import REPO_ROOT


class Weights(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    email_exact: float
    local_part_equals_handle: float
    handle_contains_local_part: float
    surname_and_initial: float
    name_jw_strong: float
    name_jw_weak: float
    employee_id_equal: float
    conflicting_verified_emails: float


class Thresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    auto_merge: float
    auto_distinct: float


class IdentityConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    weights: Weights
    thresholds: Thresholds

    @classmethod
    def load(cls, path: Path | None = None) -> IdentityConfig:
        target = path or REPO_ROOT / "config" / "identity.yaml"
        return cls.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))
