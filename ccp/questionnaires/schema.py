"""What the two model steps of the questionnaire pipeline return (spec.md A-163).

Both are tool-less calls over fenced evidence the pipeline retrieved itself. The first
extracts facts, each with a verbatim quote code checks against its chunk; the second
writes the answer's prose from those facts and, where the question maps to a tested
control, from the direction the current verdicts already decided.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FACTS_PROMPT = "questionnaire_facts_v1"
PROSE_PROMPT = "questionnaire_prose_v1"
Answer = Literal["yes", "no", "partial", "not_applicable", "insufficient_evidence"]


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(max_length=300)
    chunk_id: str
    quote: str = Field(max_length=300)


class FactsReading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[Fact] = Field(max_length=8)


class AnswerProse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Answer
    text: str = Field(max_length=1200)
