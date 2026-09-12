"""HR-3 extraction schema: is this record the named employee's, completed, and when."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ccp.evaluators.extraction.base import Cited

SCHEMA_VERSION = "hr3_fields_v1"
CompletionStatus = Literal["completed", "in_progress", "not_started", "unknown"]


class TrainingRecordFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_name_in_document: bool
    subject_name_in_document_citation: Cited | None = None
    completion_status: CompletionStatus
    completion_status_citation: Cited | None = None
    completion_date: str | None = None  # YYYY-MM-DD
    completion_date_citation: Cited | None = None
    provider: str | None = None
    provider_citation: Cited | None = None
