"""Tier evaluator registry. Higher layers register into it at import time so the evaluations
consumer can dispatch tier 3 without importing the agent package (layering: agent sits
above evaluators). A tier with no registered evaluator parks its records."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ccp.evaluators.verdict import Verdict

TierEvaluator = Callable[..., "Verdict"]
_EVALUATORS: dict[int, TierEvaluator] = {}


def register_tier(tier: int, evaluator: TierEvaluator) -> None:
    _EVALUATORS[tier] = evaluator


def evaluator_for(tier: int) -> TierEvaluator | None:
    return _EVALUATORS.get(tier)


def registered_tiers() -> list[int]:
    return sorted(_EVALUATORS)


def describe() -> dict[int, Any]:
    return {tier: getattr(fn, "__module__", str(fn)) for tier, fn in _EVALUATORS.items()}
