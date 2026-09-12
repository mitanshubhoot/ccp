"""GH-4 rule: pass when a timeline is stated with verified text; partial when a policy exists
without one; fail when there is no policy; needs_review when the deciding field is unknown."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ccp.types import VerdictStatus

if TYPE_CHECKING:
    from ccp.evaluators.rules import Decision


def decide(fields: dict[str, Any], params: dict[str, Any], clock: datetime) -> Decision:
    from ccp.evaluators.rules import Decision

    exists = fields.get("policy_exists")
    stated = fields.get("timeline_stated")
    text = fields.get("timeline_text")
    if exists is None:
        return Decision(
            VerdictStatus.needs_review,
            ("whether a disclosure policy exists could not be established from cited evidence",),
        )
    if exists is False:
        return Decision(
            VerdictStatus.failed, ("no security or vulnerability disclosure policy found",)
        )
    if stated is None:
        return Decision(
            VerdictStatus.needs_review,
            ("a policy exists but whether it states a response timeline lost its citation",),
        )
    if stated and text:
        return Decision(
            VerdictStatus.passed, (f"disclosure policy states a response timeline: {text!r}",)
        )
    if stated and not text:
        return Decision(
            VerdictStatus.needs_review,
            ("a timeline was reported but its text could not be verified against the evidence",),
        )
    return Decision(
        VerdictStatus.partial,
        ("a disclosure policy exists but states no response timeline",),
    )
