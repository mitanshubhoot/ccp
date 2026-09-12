"""Tier 1: plain rule code over structured entities. No model call anywhere in this
package; tests/unit/test_layering.py enforces it. Each module exposes subjects(ctx) and
evaluate(ctx, subject)."""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType

from ccp.evaluators.context import EvaluationContext
from ccp.evaluators.deterministic import gh1, gh2, gh3, gh5, hr1, hr2, hr5
from ccp.evaluators.verdict import Verdict

Evaluate = Callable[[EvaluationContext, str], Verdict]
Subjects = Callable[[EvaluationContext], list[str]]

MODULES: dict[str, ModuleType] = {
    "GH-1": gh1,
    "GH-2": gh2,
    "GH-3": gh3,
    "GH-5": gh5,
    "HR-1": hr1,
    "HR-2": hr2,
    "HR-5": hr5,
}


def subjects_for(ctx: EvaluationContext) -> list[str]:
    module = MODULES[ctx.spec.test_id]
    return list(module.subjects(ctx))


def evaluate(ctx: EvaluationContext, subject: str) -> Verdict:
    module = MODULES[ctx.spec.test_id]
    result: Verdict = module.evaluate(ctx, subject)
    return result
