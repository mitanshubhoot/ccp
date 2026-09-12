"""HR-3 rule: pass when the record names this person, is completed, and the completion date is
within evidence_max_age of the clock; partial when completed but stale; fail when it is
someone else's or not completed; needs_review when a deciding field is unknown."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ccp.types import VerdictStatus

if TYPE_CHECKING:
    from ccp.evaluators.rules import Decision

DEFAULT_MAX_AGE_DAYS = 365


def parse_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def decide(fields: dict[str, Any], params: dict[str, Any], clock: datetime) -> Decision:
    from ccp.evaluators.rules import Decision

    theirs = fields.get("subject_name_in_document")
    status = fields.get("completion_status")
    completed_on = parse_date(fields.get("completion_date"))
    max_age = timedelta(days=int(params.get("evidence_max_age_days") or DEFAULT_MAX_AGE_DAYS))
    if theirs is None:
        return Decision(
            VerdictStatus.needs_review,
            (
                "whether the record names this employee could not be established from"
                " cited evidence",
            ),
        )
    if theirs is False:
        return Decision(
            VerdictStatus.failed, ("the training record on file is not this employee's",)
        )
    if status is None or status == "unknown":
        return Decision(
            VerdictStatus.needs_review, ("the record's completion status is not stated",)
        )
    if status != "completed":
        return Decision(
            VerdictStatus.failed, (f"training is {status.replace('_', ' ')}, not completed",)
        )
    if completed_on is None:
        return Decision(
            VerdictStatus.needs_review,
            ("the record is marked completed but states no verifiable completion date",),
        )
    today = clock.astimezone(UTC).date()
    if completed_on + max_age < today:
        return Decision(
            VerdictStatus.partial,
            (f"training completed {completed_on.isoformat()}, older than {max_age.days} days",),
        )
    return Decision(
        VerdictStatus.passed,
        (f"training completed {completed_on.isoformat()}, within {max_age.days} days",),
    )
