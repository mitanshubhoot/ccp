"""GH-1: a person terminated in HRIS still holds an active GitHub org membership or repo
collaboration. Subject: the terminated person (canonical id). Cross-connector; depends on
identity resolution, so a pending review means needs_review."""

from __future__ import annotations

from ccp.evaluators.context import EvaluationContext
from ccp.evaluators.deterministic.common import hr_person_for, subject_key, terminated_hr_persons
from ccp.evaluators.verdict import Verdict
from ccp.types import GrantState, VerdictStatus


def subjects(ctx: EvaluationContext) -> list[str]:
    return [f"person:{ctx.canonical_of(p.entity_id)}" for p in terminated_hr_persons(ctx)]


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
    valid_from = ctx.valid_from(event_time=termination)
    name = person.display_name or person.entity_id
    if ctx.is_pending_review(canonical):
        return ctx.verdict(
            key,
            VerdictStatus.needs_review,
            f"{name} is terminated but an identity link for this person is awaiting human review",
            [person.entity_id],
            valid_from,
            ("identity_pending_review",),
        )
    github = [g for g in ctx.grants_for(canonical) if g.source_connector == "github"]
    if not github:
        return ctx.verdict(
            key,
            VerdictStatus.passed,
            f"{name} has no GitHub grants on record",
            [person.entity_id],
            valid_from,
        )
    active = [g for g in github if g.state is GrantState.active]
    seen_after = [g for g in active if g.observed_at >= termination]
    if seen_after:
        held = ", ".join(f"{g.role} on {g.asset_entity_id}" for g in seen_after)
        return ctx.verdict(
            key,
            VerdictStatus.failed,
            f"{name} was terminated {termination.date()} and still holds active GitHub access:"
            f" {held}",
            [person.entity_id, *[g.entity_id for g in seen_after]],
            valid_from,
        )
    if active:
        return ctx.verdict(
            key,
            VerdictStatus.needs_review,
            f"{name}'s GitHub grants were last observed active before the termination;"
            " no observation since",
            [person.entity_id, *[g.entity_id for g in active]],
            valid_from,
            ("no_post_termination_observation",),
        )
    closed_at = max(g.state_effective_at for g in github)
    return ctx.verdict(
        key,
        VerdictStatus.passed,
        f"every GitHub grant for {name} is revoked (last revocation {closed_at.isoformat()})",
        [person.entity_id, *[g.entity_id for g in github]],
        max(valid_from, closed_at),
    )
