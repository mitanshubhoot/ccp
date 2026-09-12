"""HR-4 extraction schemas: the critical systems named by the inventory, and the systems an
access review states as in scope. Two documents, two extractions; code compares them."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ccp.evaluators.extraction.base import Cited

SCHEMA_VERSION = "hr4_fields_v1"


class NamedSystem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    citation: Cited


class CriticalSystemsFields(BaseModel):
    """What the critical systems inventory lists."""

    model_config = ConfigDict(extra="forbid")

    systems: list[NamedSystem]


class AccessReviewFields(BaseModel):
    """What the most recent access review says it covered."""

    model_config = ConfigDict(extra="forbid")

    review_date: str | None = None  # YYYY-MM-DD
    review_date_citation: Cited | None = None
    systems_in_scope: list[NamedSystem]
