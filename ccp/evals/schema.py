"""Golden dataset schema and run report (spec.md Layer 11)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Level = Literal["component", "workflow", "e2e"]
Difficulty = Literal["easy", "hard", "edge"]
Source = Literal["manual", "synthetic"]
Grading = Literal["exact", "judge"]


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    level: Level
    difficulty: Difficulty
    source: Source
    target: str
    input: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any]
    tenant: str | None = None
    grading: Grading = "exact"
    rubric: str | None = None
    notes: str | None = None
    tags: list[str] = Field(default_factory=list)
    requires: list[Literal["anthropic", "voyage", "openai"]] = Field(default_factory=list)


class CaseResult(BaseModel):
    id: str
    level: Level
    target: str
    passed: bool
    mismatches: list[str] = Field(default_factory=list)
    latency_ms: float
    error: str | None = None
    skipped: bool = False  # a required provider key is absent; neither passed nor failed


class LevelSummary(BaseModel):
    passed: int
    total: int
    skipped: int = 0


class RunReport(BaseModel):
    run_id: str
    started_at: datetime
    finished_at: datetime
    git_commit: str | None
    golden_sha256: dict[str, str]
    cases: list[CaseResult]
    summary: dict[str, LevelSummary]

    @property
    def all_passed(self) -> bool:
        return all(c.passed or c.skipped for c in self.cases)
