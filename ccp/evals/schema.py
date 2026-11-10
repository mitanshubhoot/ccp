"""Golden dataset schema and run report (spec.md Layer 11)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Level = Literal["component", "workflow", "e2e"]
Difficulty = Literal["easy", "hard", "edge"]
Source = Literal["manual", "synthetic"]
Grading = Literal["exact", "judge"]
# fixture: written against the synthetic fixture world; customer: derived from a real trace
# or a reviewer's label. Only fixture cases may meet a judge from another family (A-103).
Origin = Literal["fixture", "customer"]


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
    rubric: str | None = None  # rubrics/<name>.md when grading == judge
    notes: str | None = None
    tags: list[str] = Field(default_factory=list)
    requires: list[Literal["anthropic", "voyage", "openai"]] = Field(default_factory=list)
    control_id: str | None = None  # adversarial: the unpoisoned control this case must match
    attack_class: str | None = None  # adversarial: the S17 class
    origin: Origin = "fixture"

    @property
    def model_backed(self) -> bool:
        """The output or its grade comes from a model: it is run k times and gated on
        bounds, never on a single flip (A-103)."""
        return "anthropic" in self.requires or self.grading == "judge"

    @property
    def customer_data(self) -> bool:
        return self.origin == "customer" or "captured" in self.tags


class CriterionResult(BaseModel):
    name: str  # c1..cn, in rubric order
    met: bool
    evidence: str = ""


class JudgeResult(BaseModel):
    criteria: list[CriterionResult]
    overall_pass: bool
    model: str | None = None
    rubric: str | None = None
    variant: str = "normal"
    guardrail_events: list[str] = Field(default_factory=list)
    usage: dict[str, Any] | None = None  # the judge call's tokens and cost


class CaseResult(BaseModel):
    id: str
    level: Level
    target: str
    passed: bool
    mismatches: list[str] = Field(default_factory=list)
    latency_ms: float
    error: str | None = None
    skipped: bool = False  # a required provider key is absent; neither passed nor failed
    judge: JudgeResult | None = None
    actual: dict[str, Any] | None = None  # the target's output, for curves and regression
    expected_fields: int = 0  # extracted_fields expectations, for the extraction suite
    attack_class: str | None = None
    attack_succeeded: bool | None = None
    cost_usd: float | None = None
    tokens: int | None = None
    trace_id: str | None = None  # the eval step id, for `ccp evals capture`
    # pass^k (A-103): a model-backed case runs k times and passes only when every run does;
    # an errored run counts as a failure.
    model_backed: bool = False
    tenant: str | None = None  # the cluster for paired comparisons
    runs: int = 1
    run_passes: int | None = None
    run_statuses: list[str | None] = Field(default_factory=list)  # the verdict status per run
    expected_status: str | None = None  # the labelled verdict, for false PASS and false FAIL
    refusals: int = 0  # runs the model refused


class LevelSummary(BaseModel):
    passed: int
    total: int
    skipped: int = 0


class ReliabilityBin(BaseModel):
    lower: float
    upper: float
    count: int
    mean_confidence: float
    accuracy: float


class LatencyPercentiles(BaseModel):
    count: int
    p50: float
    p95: float
    p99: float
    slo_p95_ms: float | None = None
    breached: bool = False


class Interval(BaseModel):
    """A rate with its 95 percent Wilson interval; n is the number of cases (runs of one
    case are not independent, so the interval is clustered by case)."""

    estimate: float
    lower: float
    upper: float
    n: int


class AITestStats(BaseModel):
    """One AI test's baseline (A-103): what the nightly gate reads."""

    target: str
    cases: int
    runs_per_case: int
    pass_at_1: Interval  # mean share of runs passing
    pass_k: Interval  # share of cases whose every run passed
    false_pass: Interval | None = None  # FAIL-labelled cases answered PASS
    false_fail: Interval | None = None  # PASS-labelled cases answered FAIL
    fail_labelled: int = 0
    pass_labelled: int = 0
    refusal_rate: float = 0.0
    cost_per_task_usd: float | None = None


class RunMetrics(BaseModel):
    reliability: list[ReliabilityBin] = Field(default_factory=list)
    ece: float | None = None
    ece_count: int = 0
    suggested_interrupt_threshold: float | None = None
    asr: dict[str, float] = Field(default_factory=dict)  # per attack class and "overall"
    asr_counts: dict[str, list[int]] = Field(default_factory=dict)  # [succeeded, attempted]
    latency: dict[str, LatencyPercentiles] = Field(default_factory=dict)  # per target
    cost_usd: dict[str, float] = Field(default_factory=dict)  # per target
    total_cost_usd: float = 0.0
    extraction: dict[str, Any] = Field(default_factory=dict)  # per schema
    judge: dict[str, Any] = Field(default_factory=dict)
    ai_tests: dict[str, AITestStats] = Field(default_factory=dict)  # per model-backed target


class RunReport(BaseModel):
    run_id: str
    started_at: datetime
    finished_at: datetime
    git_commit: str | None
    golden_sha256: dict[str, str]
    bundle_sha256: str | None = None  # prompts, tool texts, allowlists (A-100)
    cases: list[CaseResult]
    summary: dict[str, LevelSummary]
    unit: LevelSummary | None = None  # pytest tests/unit, when the run included it
    metrics: RunMetrics | None = None
    models: dict[str, str] = Field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        cases_ok = all(c.passed or c.skipped for c in self.cases)
        unit_ok = self.unit is None or self.unit.passed == self.unit.total
        return cases_ok and unit_ok
