"""No PASS over a partial or stale population (spec.md A-123).

A test whose PASS rests on an absence (no access left after a termination, no admin over
the limit, no identity without MFA) names the source scopes that absence comes from, as
`requires_complete` in knowledge/tests.yaml. Before a PASS or PARTIAL is recorded, each of
those scopes must have been seen whole by its latest pull (complete, A-118) and recently
(within ccp_sync_stale_hours, the same window that makes a connection stale, A-121).
Otherwise the verdict is withheld as needs_review, naming every gap, with the rule's own
reasoning kept after it. A FAIL stands: a violation found in part of a population is still a
violation. Rules stay pure; the runner applies this to every verdict it records.

The state of those sources is part of the evaluation key (`digest`), so a population that
goes partial or stale, or comes back whole, is a new evaluation rather than a duplicate
of the last one: the nightly full run re-decides it even when no entity changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ccp.evaluators.verdict import Verdict
from ccp.ingest.sync_runs import ScopeState
from ccp.knowledge.graph import TestSpec
from ccp.types import VerdictStatus

if TYPE_CHECKING:
    from ccp.evaluators.context import EvaluationContext

WITHHELD = (VerdictStatus.passed, VerdictStatus.partial)


def state_of(
    ref: str, sources: Mapping[str, ScopeState], now: datetime, stale_after: timedelta
) -> str:
    """How one required scope stands now: complete, stale, never pulled, or as the latest
    pull left it (partial, not_visible, failed)."""
    state = sources.get(ref)
    if state is None:
        return "never_pulled"
    if state.status != "complete":
        return state.status
    return "stale" if now - state.finished_at > stale_after else "complete"


def digest(
    spec: TestSpec, sources: Mapping[str, ScopeState], now: datetime, stale_after: timedelta
) -> str:
    """The required sources' states, for the evaluation key; empty when none is required."""
    return ",".join(
        f"{ref}={state_of(ref, sources, now, stale_after)}"
        for ref in sorted(spec.requires_complete)
    )


def gaps(ctx: EvaluationContext) -> list[tuple[str, str]]:
    """(scope, why) for every required scope not seen whole and fresh."""
    stale_after = timedelta(hours=ctx.settings.ccp_sync_stale_hours)
    out: list[tuple[str, str]] = []
    for ref in ctx.spec.requires_complete:
        state = state_of(ref, ctx.sources, ctx.now, stale_after)
        if state == "complete":
            continue
        seen = ctx.sources.get(ref)
        if state == "never_pulled":
            why = f"{ref} has no recorded pull"
        elif state == "stale" and seen is not None:
            why = f"{ref} was last seen whole at {seen.finished_at.isoformat()}"
        else:
            why = f"{ref} was {state.replace('_', ' ')} in its latest pull"
        out.append((ref, why))
    return out


def guard(ctx: EvaluationContext, verdict: Verdict) -> Verdict:
    """The verdict as recorded: a PASS or PARTIAL over a population not seen whole and fresh
    becomes needs_review; anything else is returned as it is."""
    if verdict.status not in WITHHELD or not ctx.spec.requires_complete:
        return verdict
    found = gaps(ctx)
    if not found:
        return verdict
    why = "; ".join(w for _, w in found)
    return verdict.model_copy(
        update={
            "status": VerdictStatus.needs_review,
            "reasoning": (
                f"withheld {verdict.status.value}: a source it rests on was not seen whole"
                f" and fresh ({why}). The rule said: {verdict.reasoning}"
            ),
            "guardrail_events": (
                *verdict.guardrail_events,
                *(f"incomplete_source:{ref}" for ref, _ in found),
            ),
        }
    )
