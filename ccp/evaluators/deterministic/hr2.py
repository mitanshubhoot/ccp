"""HR-2: all active human identities have MFA enabled. Subject: the tenant."""

from __future__ import annotations

from ccp.evaluators.context import EvaluationContext
from ccp.evaluators.deterministic.common import latest, subject_key
from ccp.evaluators.verdict import Verdict
from ccp.normalize.hris_mapper import IDP_ASSET_ID
from ccp.types import GrantState, VerdictStatus


def subjects(ctx: EvaluationContext) -> list[str]:
    return ["tenant"]


def evaluate(ctx: EvaluationContext, subject: str) -> Verdict:
    key = subject_key(ctx, "tenant")
    identities = [
        g for g in ctx.grants if g.asset_entity_id == IDP_ASSET_ID and g.state is GrantState.active
    ]
    humans = [g for g in identities if g.attributes.get("kind") == "human"]
    missing = [g for g in humans if not g.attributes.get("mfa_enabled")]
    observed = latest(*(g.observed_at for g in identities)) or ctx.trigger_observed_at
    valid_from = ctx.valid_from(evidence_observed_at=observed)
    refs = [g.entity_id for g in identities]
    if not identities:
        return ctx.verdict(
            key, VerdictStatus.needs_review, "no identity observations", refs, valid_from
        )
    if missing:
        who = ", ".join(str(g.attributes.get("email")) for g in missing)
        return ctx.verdict(
            key,
            VerdictStatus.failed,
            f"{len(missing)} of {len(humans)} active human identities lack MFA: {who}",
            refs,
            valid_from,
        )
    return ctx.verdict(
        key,
        VerdictStatus.passed,
        f"all {len(humans)} active human identities have MFA enabled",
        refs,
        valid_from,
    )
