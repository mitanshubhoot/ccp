"""GH-4 extraction schema: does a disclosure policy exist, and does it state a timeline."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ccp.evaluators.extraction.base import Cited

SCHEMA_VERSION = "gh4_fields_v1"


class DisclosurePolicyFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_exists: bool
    policy_exists_citation: Cited | None = None
    timeline_stated: bool
    timeline_stated_citation: Cited | None = None
    timeline_text: str | None = None
    timeline_text_citation: Cited | None = None
