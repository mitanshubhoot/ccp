"""GH-5: no API keys or secrets in plaintext in tracked evidence. Subject: each repo asset.
The scan covers every evidence row attached to that repo regardless of type; a repo with
no file listing observed at all is needs_review, not pass. A pass also needs every listed file
read: a listing with files not read, or entries not listed (directories the connector did not
descend into), is needs_review, because the secret may sit in what was not read (A-96). A
secret found in what was read is a FAIL either way."""

from __future__ import annotations

from ccp.evaluators.context import EvaluationContext
from ccp.evaluators.deterministic.common import latest, subject_key
from ccp.evaluators.verdict import Verdict
from ccp.normalize.entities import Evidence
from ccp.normalize.github_mapper import NOT_INLINED, UNREAD_EVENT_PREFIXES
from ccp.security.secrets_patterns import scan
from ccp.types import EvidenceType, VerdictStatus


def subjects(ctx: EvaluationContext) -> list[str]:
    return sorted(f"repo:{a.entity_id}" for a in ctx.assets.values() if a.asset_type == "repo")


def _unread(listing: Evidence) -> list[str]:
    """What the listing says was not read: the mapper's events, or for rows written before
    those events existed, the not-inlined marker in the text."""
    events = [e for e in listing.guardrail_events if e.startswith(UNREAD_EVENT_PREFIXES)]
    if events:
        return events
    count = listing.text.count(NOT_INLINED)
    return [f"files_not_read:{count}"] if count else []


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
    listings = [e for e in rows if e.evidence_type is EvidenceType.repo_file_listing]
    if not listings:
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
    unread = [event for listing in listings for event in _unread(listing)]
    if unread:
        return ctx.verdict(
            key,
            VerdictStatus.needs_review,
            f"no plaintext secrets in what was read for {name}, but not every file was read"
            f" ({', '.join(unread)})",
            refs,
            valid_from,
            ("incomplete_file_read",),
        )
    return ctx.verdict(
        key,
        VerdictStatus.passed,
        f"no plaintext secrets across {len(rows)} evidence row(s) for {name}; every listed file"
        " was read",
        refs,
        valid_from,
    )
