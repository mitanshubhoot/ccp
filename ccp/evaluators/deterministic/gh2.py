"""GH-2: org admin count is within the tenant's configured limit. Subject: the org asset."""

from __future__ import annotations

from ccp.evaluators.context import EvaluationContext
from ccp.evaluators.deterministic.common import latest, subject_key
from ccp.evaluators.verdict import Verdict
from ccp.types import GrantState, VerdictStatus


def subjects(ctx: EvaluationContext) -> list[str]:
    return [f"org:{a.entity_id}" for a in ctx.assets.values() if a.asset_type == "org"]


def evaluate(ctx: EvaluationContext, subject: str) -> Verdict:
    asset_id = subject.removeprefix("org:")
    key = subject_key(ctx, subject)
    limit_key = str(ctx.spec.parameters.get("limit_key", "org_admin_limit"))
    limit = int(getattr(ctx.tenant.settings, limit_key))
    admins = [
        g
        for g in ctx.grants
        if g.asset_entity_id == asset_id and g.state is GrantState.active and g.role == "admin"
    ]
    observed = latest(*(g.observed_at for g in admins)) or ctx.trigger_observed_at
    valid_from = ctx.valid_from(evidence_observed_at=observed)
    refs = [asset_id, *[g.entity_id for g in admins]]
    who = ", ".join(sorted(g.person_entity_id.rsplit(":", 1)[-1] for g in admins))
    if len(admins) > limit:
        return ctx.verdict(
            key,
            VerdictStatus.failed,
            f"{len(admins)} organization admins ({who}) exceed the limit of {limit}",
            refs,
            valid_from,
        )
    return ctx.verdict(
        key,
        VerdictStatus.passed,
        f"{len(admins)} organization admins ({who}) within the limit of {limit}",
        refs,
        valid_from,
    )
