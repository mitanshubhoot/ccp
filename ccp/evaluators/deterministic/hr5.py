"""HR-5: no shared or generic account exists without a documented individual owner who is
an active employee. Subject: the tenant. (The same unowned account is the one identity
resolution refuses to merge: an account with no known owner is never attributed by
similarity.) With no account registry observed at all there is nothing to pass on."""

from __future__ import annotations

from ccp.evaluators.context import EvaluationContext
from ccp.evaluators.deterministic.common import latest, subject_key
from ccp.evaluators.verdict import Verdict
from ccp.types import AccountKind, EmploymentStatus, EvidenceType, VerdictStatus


def subjects(ctx: EvaluationContext) -> list[str]:
    return ["tenant"]


def evaluate(ctx: EvaluationContext, subject: str) -> Verdict:
    key = subject_key(ctx, "tenant")
    registry = [e for e in ctx.evidence if e.evidence_type is EvidenceType.account_registry]
    shared = sorted(
        (p for p in ctx.persons.values() if p.account_kind is AccountKind.shared),
        key=lambda p: p.entity_id,
    )
    active_employees = {
        p.employee_id
        for p in ctx.persons.values()
        if p.employee_id and p.employment_status is EmploymentStatus.active
    }
    unowned = [
        p for p in shared if not p.owner_employee_id or p.owner_employee_id not in active_employees
    ]
    observed = latest(*(e.observed_at for e in registry)) or ctx.trigger_observed_at
    valid_from = ctx.valid_from(evidence_observed_at=observed)
    refs = [p.entity_id for p in shared] + [e.entity_id for e in registry]
    if not registry:
        return ctx.verdict(
            key,
            VerdictStatus.needs_review,
            "no account registry observed for this tenant",
            refs,
            valid_from,
            ("no_account_registry",),
        )
    if unowned:
        names = ", ".join(
            f"{p.display_name} (owner {p.owner_employee_id or 'none recorded'})" for p in unowned
        )
        return ctx.verdict(
            key,
            VerdictStatus.failed,
            f"{len(unowned)} shared account(s) without an active individual owner: {names}",
            refs,
            valid_from,
        )
    return ctx.verdict(
        key,
        VerdictStatus.passed,
        f"all {len(shared)} shared account(s) have an active individual owner",
        refs,
        valid_from,
    )
