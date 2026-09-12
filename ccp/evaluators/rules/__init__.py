"""Pure decision rules for the AI tiers (spec.md R5): `decide(fields, params, clock)` over
verified field values. A field that lost its citation arrives as None and the rule says
what that means; no prose in a prompt ever decides a status."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ccp.evaluators.rules import gh4, hr3
from ccp.types import VerdictStatus


@dataclass(frozen=True)
class Decision:
    status: VerdictStatus
    reasons: tuple[str, ...]


Rule = Callable[[dict[str, Any], dict[str, Any], datetime], Decision]

RULES: dict[str, Rule] = {
    "gh4_disclosure_timeline": gh4.decide,
    "hr3_training_current": hr3.decide,
}


def decide(rule: str, fields: dict[str, Any], params: dict[str, Any], clock: datetime) -> Decision:
    try:
        fn = RULES[rule]
    except KeyError:
        raise KeyError(f"unknown rule {rule!r}; known: {sorted(RULES)}") from None
    return fn(fields, params, clock)


__all__ = ["RULES", "Decision", "decide"]
