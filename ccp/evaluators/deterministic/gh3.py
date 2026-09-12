"""GH-3: the default branch requires review before merge. Subject: each repo asset.
pass: protected, >= 1 approving review, admins included; partial: reviews required but
admins exempt; fail: unprotected or no review requirement; needs_review: no protection
snapshot observed."""

from __future__ import annotations

from ccp.evaluators.context import EvaluationContext
from ccp.evaluators.deterministic.common import subject_key
from ccp.evaluators.verdict import Verdict
from ccp.types import VerdictStatus


def subjects(ctx: EvaluationContext) -> list[str]:
    return sorted(f"repo:{a.entity_id}" for a in ctx.assets.values() if a.asset_type == "repo")


def evaluate(ctx: EvaluationContext, subject: str) -> Verdict:
    asset_id = subject.removeprefix("repo:")
    key = subject_key(ctx, subject)
    asset = ctx.assets.get(asset_id)
    if asset is None:
        return ctx.verdict(
            key,
            VerdictStatus.needs_review,
            "repo asset not found",
            [asset_id],
            ctx.trigger_observed_at,
        )
    protection = asset.settings.get("branch_protection")
    valid_from = ctx.valid_from(evidence_observed_at=asset.observed_at)
    if not isinstance(protection, dict):
        return ctx.verdict(
            key,
            VerdictStatus.needs_review,
            f"no branch protection observation for {asset.name}",
            [asset.entity_id],
            valid_from,
            ("no_protection_snapshot",),
        )
    reviews = int(protection.get("required_approving_review_count") or 0)
    enforce = bool(protection.get("enforce_admins"))
    branch = protection.get("branch")
    refs = [asset.entity_id]
    if not protection.get("protected") or reviews < 1:
        return ctx.verdict(
            key,
            VerdictStatus.failed,
            f"{asset.name} default branch {branch} does not require an approving review"
            " before merge",
            refs,
            valid_from,
        )
    if not enforce:
        return ctx.verdict(
            key,
            VerdictStatus.partial,
            f"{asset.name} requires {reviews} approving review(s) on {branch} but"
            " administrators are exempt",
            refs,
            valid_from,
        )
    return ctx.verdict(
        key,
        VerdictStatus.passed,
        f"{asset.name} requires {reviews} approving review(s) on {branch} with administrators"
        " included",
        refs,
        valid_from,
    )
