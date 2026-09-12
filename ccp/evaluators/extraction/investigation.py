"""The Investigator's structured-output schema (spec.md Layer 14): what the model fills.
A closed action enum, explicit target fields (structured output needs a closed object, not
a free dict), and no reversible/live flags, which the catalogue supplies. Lives with the
other extraction schemas so the agent layer can import it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ccp.evaluators.extraction.base import Cited
from ccp.knowledge.actions import ActionName
from ccp.types import EvidenceType

Category = Literal[
    "offboarding_gap",
    "missing_evidence",
    "stale_evidence",
    "configuration",
    "ownership",
    "identity_unresolved",
    "unknown",
]
PROMPT_VERSION = "investigator_diagnose_v1"
GATHER_PROMPT_VERSION = "investigator_gather_v1"
SCHEMA_VERSION = "investigation_v1"


class MissingEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_type: EvidenceType
    subject_key: str | None = None
    why_needed: str


class ProposalTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str | None = None
    org: str | None = None
    login: str | None = None
    subject_key: str | None = None
    evidence_type: str | None = None

    def as_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.model_dump().items() if v}


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ActionName
    target: ProposalTarget
    rationale: str


class InvestigationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_cause: str
    root_cause_category: Category
    missing_evidence: list[MissingEvidence]
    proposed_actions: list[ProposedAction]
    citations: list[Cited]
