"""The Policy Agent's structured-output schema (spec.md Layer 15): one gap per requirement the
control maps to, with verified citations when covered."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ccp.evaluators.extraction.base import Cited

GAP_PROMPT_VERSION = "policy_gap_v1"
GATHER_PROMPT_VERSION = "policy_gather_v1"
COMPOSE_PROMPT_VERSION = "policy_compose_v1"
SCHEMA_VERSION = "gap_report_v1"


class PolicyGapDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement_id: str  # "soc2:CC7.1" or "iso27001:A.8.8"
    covered: bool
    citations: list[Cited]  # verbatim quotes from the policy text that address it
    gap_description: str | None = None  # what is missing when not covered


class GapReportDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gaps: list[PolicyGapDraft]
    summary: str
