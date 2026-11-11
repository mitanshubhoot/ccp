"""What a task item's read run returns (spec.md A-151, report 6.7 "Sub-agent context
isolation"): one keyed reading per item it was given, each a closed answer, a one-line
reason, at most eight claims each citing a chunk, and what evidence would settle it. Code
checks every citation and the size before anything merges into an item row; the parent
task sees only this, never the run's transcript.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ccp.evaluators.extraction.base import Cited

PROMPT_VERSION = "task_item_answer_v1"
GATHER_PROMPT_VERSION = "task_item_gather_v1"
SCHEMA_VERSION = "task_item_v1"
RESULT_TOKEN_CAP = 1500  # one item's merged result, as the parent sees it
MAX_FINDINGS = 8

Answer = Literal["yes", "no", "partial", "insufficient_evidence"]


class ItemFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(max_length=300)
    citation: Cited


class KeyedReading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    answer: Answer
    reason: str = Field(max_length=300)  # one line
    findings: list[ItemFinding] = Field(default_factory=list, max_length=MAX_FINDINGS)
    missing: list[str] = Field(default_factory=list, max_length=5)  # what would settle it


class ItemReadings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readings: list[KeyedReading]
