"""The adequacy verifier's answer (spec.md A-162): accept or flag one uploaded document for
the controls it is evidence for, each reason citing segment ids, never quoting."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROMPT_VERSION = "adequacy_v1"
SCHEMA_VERSION = "adequacy_v1"
AdequacyCode = Literal[
    "shows_control",  # an accept's reason: these segments show the control operating
    "wrong_period",
    "wrong_subject",
    "incomplete",
    "not_this_control",
    "unsigned",
    "unreadable",
    "other",
]


class AdequacyReason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: AdequacyCode
    segment_ids: list[str] = Field(max_length=8)
    note: str = Field(max_length=300)


class AdequacyReading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "flag"]
    reasons: list[AdequacyReason] = Field(max_length=6)
    summary: str = Field(max_length=400)
