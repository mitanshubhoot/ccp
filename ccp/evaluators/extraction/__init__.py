"""Extraction schemas (spec.md R5): what the model fills. Every cited field `x` has a
sibling `x_citation`; the model never returns a status or a confidence."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel

from ccp.evaluators.extraction import gh4, hr3, hr4
from ccp.evaluators.extraction.base import Cited


@dataclass(frozen=True)
class SchemaMeta:
    name: str
    model: type[BaseModel]
    version: str
    required: tuple[str, ...]  # coverage denominator
    cited: tuple[str, ...]  # fields that carry a citation sibling
    value_in_quote: tuple[str, ...]  # fields whose value must appear in the quote


SCHEMAS: dict[str, SchemaMeta] = {
    "DisclosurePolicyFields": SchemaMeta(
        name="DisclosurePolicyFields",
        model=gh4.DisclosurePolicyFields,
        version=gh4.SCHEMA_VERSION,
        required=("policy_exists", "timeline_stated"),
        cited=("policy_exists", "timeline_stated", "timeline_text"),
        value_in_quote=("timeline_text",),
    ),
    "TrainingRecordFields": SchemaMeta(
        name="TrainingRecordFields",
        model=hr3.TrainingRecordFields,
        version=hr3.SCHEMA_VERSION,
        required=("subject_name_in_document", "completion_status", "completion_date"),
        cited=("subject_name_in_document", "completion_status", "completion_date", "provider"),
        value_in_quote=("completion_date", "provider"),
    ),
    # list-valued schemas: the tier 3 graph verifies each list item's citation itself
    "CriticalSystemsFields": SchemaMeta(
        name="CriticalSystemsFields",
        model=hr4.CriticalSystemsFields,
        version=hr4.SCHEMA_VERSION,
        required=("systems",),
        cited=(),
        value_in_quote=(),
    ),
    "AccessReviewFields": SchemaMeta(
        name="AccessReviewFields",
        model=hr4.AccessReviewFields,
        version=hr4.SCHEMA_VERSION,
        required=("systems_in_scope",),
        cited=("review_date",),
        value_in_quote=("review_date",),
    ),
}


def schema_for(name: str) -> SchemaMeta:
    try:
        return SCHEMAS[name]
    except KeyError:
        raise KeyError(f"unknown extraction schema {name!r}; known: {sorted(SCHEMAS)}") from None


def _agent_schemas() -> dict[str, type[BaseModel]]:
    """The agent graphs' structured-output schemas, by class name (imported lazily so the
    package stays importable from the graph modules that define their own metadata)."""
    from ccp.evaluators.extraction.copilot import CopilotAnswerDraft, IntentDraft
    from ccp.evaluators.extraction.investigation import InvestigationDraft
    from ccp.evaluators.extraction.policy import GapReportDraft

    return {
        cls.__name__: cls
        for cls in (InvestigationDraft, GapReportDraft, IntentDraft, CopilotAnswerDraft)
    }


def schema_block(model: type[BaseModel]) -> str:
    """The system block that carries a schema (tier 2, every structured node, replay)."""
    return "Extraction schema (JSON Schema):\n" + json.dumps(
        model.model_json_schema(), sort_keys=True
    )


def schema_model(name: str) -> type[BaseModel] | None:
    """Any structured-output schema the platform sends, by class name (replay needs every
    one, not only the tier 2 and tier 3 extraction schemas)."""
    meta = SCHEMAS.get(name)
    if meta is not None:
        return meta.model
    return _agent_schemas().get(name)


__all__ = ["SCHEMAS", "Cited", "SchemaMeta", "schema_block", "schema_for", "schema_model"]
