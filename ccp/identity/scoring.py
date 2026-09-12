"""Gate, then score, then route (spec.md Layer 4).

    pair ──► gate ──► blocked (kind mismatch with a service or shared account)
              │
              └─► score = clamp(Σ matched weights, 0, 1)
                    ├── >= auto_merge      -> auto_merge   (never when either kind is unknown)
                    ├── <= auto_distinct   -> auto_distinct
                    └── otherwise          -> review

The gate runs before any feature is computed: no weight tuning can ever merge a human
with a service or shared account. Every feature value is returned so the decision log
carries exactly what was compared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rapidfuzz.distance import JaroWinkler

from ccp.identity.blocking import fold, local_part, name_key, normalize_email
from ccp.identity.config import IdentityConfig
from ccp.normalize.entities import Person
from ccp.types import AccountKind

GateResult = Literal["passed", "blocked_kind_mismatch", "blocked_shared_account"]
Route = Literal["auto_merge", "auto_distinct", "review", "blocked"]


@dataclass(frozen=True)
class Scored:
    gate_result: GateResult
    features: dict[str, float | bool]
    score: float | None
    route: Route


def gate(left: Person, right: Person) -> GateResult:
    kinds = {left.account_kind, right.account_kind}
    if left.account_kind is right.account_kind:
        return "passed"
    if AccountKind.shared in kinds:
        return "blocked_shared_account"
    if AccountKind.service in kinds:
        return "blocked_kind_mismatch"
    return "passed"


def _handles(person: Person) -> set[str]:
    return {h.lower() for h in person.handles.values()}


def _locals(person: Person) -> set[str]:
    return {local_part(e) for e in person.emails}


def features(left: Person, right: Person) -> dict[str, float | bool]:
    left_emails = {normalize_email(e) for e in left.emails}
    right_emails = {normalize_email(e) for e in right.emails}
    email_exact = bool(left_emails & right_emails)
    conflicting = bool(left_emails) and bool(right_emails) and not email_exact

    locals_l, locals_r = _locals(left), _locals(right)
    handles_l, handles_r = _handles(left), _handles(right)
    handle_locals_l = {h.replace(".", "") for h in handles_l}
    handle_locals_r = {h.replace(".", "") for h in handles_r}
    local_equals_handle = bool(locals_l & handle_locals_r) or bool(locals_r & handle_locals_l)
    handle_contains = (not local_equals_handle) and (
        any(lp in h and len(lp) >= 4 for lp in locals_l for h in handle_locals_r)
        or any(lp in h and len(lp) >= 4 for lp in locals_r for h in handle_locals_l)
    )

    nk_l, nk_r = name_key(left.display_name), name_key(right.display_name)
    surname_and_initial = bool(nk_l and nk_r and nk_l == nk_r)
    jw = 0.0
    if left.display_name and right.display_name:
        jw = float(JaroWinkler.similarity(fold(left.display_name), fold(right.display_name)))

    employee_equal = bool(
        left.employee_id and right.employee_id and left.employee_id == right.employee_id
    )
    return {
        "email_exact": email_exact,
        "local_part_equals_handle": local_equals_handle,
        "handle_contains_local_part": handle_contains,
        "surname_and_initial": surname_and_initial,
        "name_jaro_winkler": round(jw, 4),
        "employee_id_equal": employee_equal,
        "conflicting_verified_emails": conflicting,
    }


def score(feats: dict[str, float | bool], config: IdentityConfig) -> float:
    w = config.weights
    total = 0.0
    if feats["email_exact"]:
        total += w.email_exact
    if feats["local_part_equals_handle"]:
        total += w.local_part_equals_handle
    if feats["handle_contains_local_part"]:
        total += w.handle_contains_local_part
    if feats["surname_and_initial"]:
        total += w.surname_and_initial
    jw = float(feats["name_jaro_winkler"])
    if jw >= 0.90:
        total += w.name_jw_strong
    elif jw >= 0.80:
        total += w.name_jw_weak
    if feats["employee_id_equal"]:
        total += w.employee_id_equal
    if feats["conflicting_verified_emails"]:
        total += w.conflicting_verified_emails
    return max(0.0, min(1.0, round(total, 4)))


def route(value: float, left: Person, right: Person, config: IdentityConfig) -> Route:
    if value >= config.thresholds.auto_merge:
        if AccountKind.unknown in (left.account_kind, right.account_kind):
            return "review"  # an unknown kind can never auto-merge
        return "auto_merge"
    if value <= config.thresholds.auto_distinct:
        return "auto_distinct"
    return "review"


def evaluate(left: Person, right: Person, config: IdentityConfig) -> Scored:
    gate_result = gate(left, right)
    if gate_result != "passed":
        return Scored(gate_result, {}, None, "blocked")
    feats = features(left, right)
    value = score(feats, config)
    return Scored(gate_result, feats, value, route(value, left, right, config))
