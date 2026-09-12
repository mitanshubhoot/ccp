"""GH-5: no API keys or secrets in plaintext in tracked evidence. Subject: each repo asset.
The scan covers every evidence row attached to that repo regardless of type; a repo with
no file listing observed at all is needs_review, not pass."""

from __future__ import annotations

from ccp.evaluators.context import EvaluationContext
from ccp.evaluators.deterministic.common import latest, subject_key
from ccp.evaluators.verdict import Verdict
from ccp.security.secrets_patterns import scan
from ccp.types import EvidenceType, VerdictStatus


def subjects(ctx: EvaluationContext) -> list[str]:
    return sorted(f"repo:{a.entity_id}" for a in ctx.assets.values() if a.asset_type == "repo")


def evaluate(ctx: EvaluationContext, subject: str) -> Verdict:
    asset_id = subject.removeprefix("repo:")
    key = subject_key(ctx, subject)
    asset = ctx.assets.get(asset_id)
    name = asset.name if asset else asset_id
    rows = sorted(
        (e for e in ctx.evidence if e.subject_entity_id == asset_id), key=lambda e: e.entity_id
    )
    observed = latest(*(e.observed_at for e in rows)) or ctx.trigger_observed_at
    valid_from = ctx.valid_from(evidence_observed_at=observed)
    refs = [asset_id, *[e.entity_id for e in rows]]
    if not any(e.evidence_type is EvidenceType.repo_file_listing for e in rows):
        return ctx.verdict(
            key,
            VerdictStatus.needs_review,
            f"no tracked-file listing observed for {name}; {len(rows)} other evidence row(s)",
            refs,
            valid_from,
            ("no_file_listing",),
        )
    findings = []
    for row in rows:
        for hit in scan(row.text):
            findings.append(f"{hit.pattern} in {row.title} line {hit.line} ({hit.preview})")
    if findings:
        return ctx.verdict(
            key,
            VerdictStatus.failed,
            f"plaintext secret(s) in {name}: " + "; ".join(findings),
            refs,
            valid_from,
            ("secret_detected",),
        )
    return ctx.verdict(
        key,
        VerdictStatus.passed,
        f"no plaintext secrets across {len(rows)} evidence row(s) for {name}",
        refs,
        valid_from,
    )
