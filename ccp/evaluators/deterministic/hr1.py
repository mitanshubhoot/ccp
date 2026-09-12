"""HR-1: a terminated person has zero active access grants across every connected system
within the SLA of the termination event. The core cross-system case (spec.md A-23).

    for each grant in the person's canonical group
      revoked and effective <= deadline   -> fine
      revoked and effective  > deadline   -> violation (revoked late)
      active and observed   >= deadline   -> violation (still active after the SLA)
      active and observed    < deadline   -> unknown (no observation after the deadline)
    fail if any violation; else needs_review if any unknown; else pass
"""

from __future__ import annotations

from ccp.evaluators.context import EvaluationContext
from ccp.evaluators.deterministic.common import hr_person_for, subject_key, terminated_hr_persons
from ccp.evaluators.verdict import Verdict
from ccp.normalize.entities import AccessGrant
from ccp.types import GrantState, VerdictStatus


def subjects(ctx: EvaluationContext) -> list[str]:
    return [f"person:{ctx.canonical_of(p.entity_id)}" for p in terminated_hr_persons(ctx)]


def _describe(grant: AccessGrant) -> str:
    what = f"{grant.role} on {grant.asset_entity_id} ({grant.source_connector})"
    if grant.state is GrantState.active:
        return f"{what} still active"
    return f"{what} revoked late at {grant.state_effective_at.isoformat()}"


def evaluate(ctx: EvaluationContext, subject: str) -> Verdict:
    canonical = subject.removeprefix("person:")
    key = subject_key(ctx, subject)
    person = hr_person_for(ctx, canonical)
    if person is None or person.status_effective_at is None:
        return ctx.verdict(
            key,
            VerdictStatus.needs_review,
            "no HR termination record for this person",
            [canonical],
            ctx.trigger_observed_at,
        )
    termination = person.status_effective_at
    deadline = termination + ctx.sla
    valid_from = ctx.valid_from(event_time=termination)
    name = person.display_name or person.entity_id
    if ctx.is_pending_review(canonical):
        return ctx.verdict(
            key,
            VerdictStatus.needs_review,
            f"{name} is terminated but an identity link is awaiting human review",
            [person.entity_id],
            valid_from,
            ("identity_pending_review",),
        )
    grants = ctx.grants_for(canonical)
    violations: list[AccessGrant] = []
    unknown: list[AccessGrant] = []
    for grant in grants:
        if grant.state is GrantState.revoked:
            if grant.state_effective_at > deadline:
                violations.append(grant)
        elif grant.observed_at >= deadline:
            violations.append(grant)
        else:
            unknown.append(grant)
    refs = [person.entity_id, *[g.entity_id for g in violations + unknown]]
    if violations:
        return ctx.verdict(
            key,
            VerdictStatus.failed,
            f"{name} was terminated {termination.isoformat()} with a {ctx.sla} SLA"
            f" (deadline {deadline.isoformat()}): " + "; ".join(_describe(g) for g in violations),
            refs,
            valid_from,
        )
    if unknown:
        return ctx.verdict(
            key,
            VerdictStatus.needs_review,
            f"{name}: {len(unknown)} grant(s) last observed active before the SLA deadline"
            " and not observed since",
            refs,
            valid_from,
            ("no_post_sla_observation",),
        )
    return ctx.verdict(
        key,
        VerdictStatus.passed,
        f"every grant for {name} was revoked by the SLA deadline {deadline.isoformat()}",
        [person.entity_id, *[g.entity_id for g in grants]],
        valid_from,
    )
