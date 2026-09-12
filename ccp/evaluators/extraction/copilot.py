"""The Copilot's structured-output schemas (spec.md Layer 17): the intent of a question, and
the answer with its citations and requested actions. Requested actions reuse the Investigator's
closed action enum and explicit target object, so the same catalogue bounds both agents."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ccp.evaluators.extraction.base import Cited
from ccp.evaluators.extraction.investigation import ProposalTarget
from ccp.knowledge.actions import ActionName

Intent = Literal["explain", "status", "missing", "act", "other"]
INTENTS: tuple[str, ...] = ("explain", "status", "missing", "act", "other")
CLASSIFY_PROMPT_VERSION = "copilot_classify_v1"
GATHER_PROMPT_VERSION = "copilot_gather_v1"
ANSWER_PROMPT_VERSION = "copilot_answer_v1"
SCHEMA_VERSION = "copilot_answer_v1"


class IntentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    test_id: str | None = None  # "HR-1" when the question names a test
    subject_hint: str | None = None  # a person, repository, or system the question names
    framework_id: str | None = None  # "soc2" | "iso27001" when the question names one


class RequestedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ActionName
    target: ProposalTarget
    rationale: str
    verdict_id: str | None = None  # the verdict the action would fix


class CopilotAnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    citations: list[Cited]
    requested_actions: list[RequestedAction]
