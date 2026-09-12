"""HR-4 rule: missing = critical - in_scope. pass when nothing is missing; partial when some
critical systems are missing (each named); fail when the review covers none of them;
needs_review when either list is unknown."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ccp.types import VerdictStatus

if TYPE_CHECKING:
    from ccp.evaluators.rules import Decision

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    return _NON_ALNUM.sub("", name.lower())


def compare(critical: list[str], in_scope: list[str]) -> tuple[list[str], list[str]]:
    """(missing critical systems, covered critical systems), in inventory order."""
    covered_keys = {normalize_name(s) for s in in_scope}
    missing = [s for s in critical if normalize_name(s) not in covered_keys]
    covered = [s for s in critical if normalize_name(s) in covered_keys]
    return missing, covered


def decide(fields: dict[str, Any], params: dict[str, Any], clock: datetime) -> Decision:
    from ccp.evaluators.rules import Decision

    critical = fields.get("critical_systems")
    in_scope = fields.get("systems_in_scope")
    if critical is None:
        return Decision(
            VerdictStatus.needs_review, ("the critical systems inventory could not be read",)
        )
    if in_scope is None:
        return Decision(
            VerdictStatus.needs_review, ("the most recent access review's scope could not be read",)
        )
    if not critical:
        return Decision(VerdictStatus.needs_review, ("the inventory names no critical systems",))
    missing, covered = compare(list(critical), list(in_scope))
    review_date = fields.get("review_date")
    dated = f" (review dated {review_date})" if review_date else ""
    if not missing:
        return Decision(
            VerdictStatus.passed,
            (
                "the most recent access review covers every critical system: "
                f"{', '.join(covered)}{dated}",
            ),
        )
    if not covered:
        return Decision(
            VerdictStatus.failed,
            (
                "the most recent access review covers none of the critical systems;"
                f" missing: {', '.join(missing)}{dated}",
            ),
        )
    return Decision(
        VerdictStatus.partial,
        (
            f"the most recent access review omits {len(missing)} of {len(critical)} critical"
            f" systems: {', '.join(missing)}{dated}",
        ),
    )
