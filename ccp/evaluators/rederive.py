"""Re-deriving a tier-1 verdict from what it read (spec.md A-125).

A tier-1 verdict cited entity ids but could not be re-derived: its rule had read the latest
observations, and those move on. Every evaluation now reads as of a watermark (the tenant's
observation counter, stamped under a lock at insert, A-125), and its verdict carries a
manifest of what it read: the watermark, its clock, the sync runs behind its required
sources, and the tenant parameters the rules read. `rederive` loads that same context and
runs the same rule, and compares what it gets with what was stored:

    exact          every compared field is the same
    rule_changed   something differs, and so does the rule stamp: the rule's code or its
                   parameters changed since, which is a correction's preview, not a fault
    mismatch       something differs under the same rule: the verdict cannot be reproduced

The nightly sentinel re-derives a sample (the newest verdict of each subject) and appends
each outcome to `replay_checks`; any mismatch is a defect to chase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from ulid import ULID

from ccp.config import Settings, TenantSettings
from ccp.evaluators import completeness, deterministic
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.context import load_context
from ccp.evaluators.verdict import Verdict
from ccp.ingest import sync_runs
from ccp.knowledge import graph as knowledge
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

Outcome = Literal["exact", "rule_changed", "mismatch"]
COMPARED = (
    "evaluation_key",
    "status",
    "reasoning",
    "evidence_refs",
    "valid_from",
    "guardrail_events",
    "rule_version",
)


@dataclass(frozen=True)
class Rederivation:
    verdict_id: str
    test_id: str
    subject_key: str
    outcome: Outcome
    diff: tuple[str, ...] = ()
    derived: Verdict | None = field(default=None, compare=False, repr=False)


def rederive(
    db: ScopedDB,
    settings: Settings,
    verdict: Verdict,
    parameters: Literal["recorded", "current"] = "recorded",
) -> Rederivation:
    """The verdict re-decided from what it read. With the recorded parameters it should be
    exact; with the tenant's current ones it is what a correction would record (A-128)."""
    if verdict.tier != 1 or not verdict.manifest:
        raise LookupError(f"{verdict.verdict_id} is not a tier-1 verdict with an input manifest")
    spec = knowledge.load().tests[verdict.test_id]
    manifest = verdict.manifest
    recorded = manifest.get("settings", {}) if parameters == "recorded" else {}
    tenant_settings = TenantSettings.model_validate({**db.tenant.settings.model_dump(), **recorded})
    ctx = load_context(
        db,
        settings,
        spec,
        verdict.trigger_observed_at,
        datetime.fromisoformat(str(manifest["now"])),
        reason=verdict.trigger_reason,
        as_of=int(manifest["watermark"]),
        sources=sync_runs.scopes_at(db, manifest.get("sources", {})),
    )
    ctx = replace(ctx, tenant=TenantContext(db.tenant.tenant_id, tenant_settings))
    derived = completeness.guard(ctx, deterministic.evaluate(ctx, verdict.subject))
    diff = tuple(
        f"{name}: {getattr(verdict, name)!r} -> {getattr(derived, name)!r}"
        for name in COMPARED
        if getattr(verdict, name) != getattr(derived, name)
    )
    outcome: Outcome = "exact"
    if diff:
        outcome = "rule_changed" if verdict.rule_version != derived.rule_version else "mismatch"
    return Rederivation(
        verdict.verdict_id, verdict.test_id, verdict.subject_key, outcome, diff, derived
    )


def record(db: ScopedDB, result: Rederivation, at: datetime | None = None) -> None:
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO replay_checks"
            " (tenant_id, check_id, verdict_id, test_id, checked_at, outcome, diff)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                db.tenant.tenant_id,
                str(ULID()),
                result.verdict_id,
                result.test_id,
                at or datetime.now(UTC),
                result.outcome,
                json.dumps(list(result.diff)),
            ),
        )


def sentinel(db: ScopedDB, settings: Settings, sample: int = 25) -> list[Rederivation]:
    """The nightly job: re-derive the newest tier-1 verdict of up to `sample` subjects and
    record each outcome."""
    rows = db.fetch_all(
        "SELECT verdict_id FROM (SELECT DISTINCT ON (subject_key) verdict_id, created_at"
        " FROM verdicts WHERE tier = 1 AND manifest IS NOT NULL"
        " ORDER BY subject_key, created_at DESC, verdict_id DESC) latest"
        " ORDER BY created_at DESC, verdict_id DESC LIMIT %s",
        (sample,),
    )
    out: list[Rederivation] = []
    for (verdict_id,) in rows:
        verdict = verdicts.load(db, str(verdict_id))
        if verdict is None:  # pragma: no cover - the row was just read
            continue
        result = rederive(db, settings, verdict)
        record(db, result)
        out.append(result)
    return out
