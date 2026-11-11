"""Investigations written by code when the rule explains the failure (spec.md A-159).

A failing deterministic check already says what is wrong and with which resources; the
lookups (A-158) say who changed each one, when, and where it lives in code. For such a
verdict nothing is left for a model to diagnose: code writes the investigation, renders the
catalogue's fixes for the test from the failing resources, and offers each one that passes
its offline checks as a suggestion. No model key is needed, and nothing parks.
"""

from __future__ import annotations

from typing import Any

from ccp.evaluators.verdict import Verdict
from ccp.investigation import schema
from ccp.investigation.schema import Investigation
from ccp.lookups import store as lookup_store
from ccp.lookups.store import Lookup
from ccp.remediation import fixes, suggestions
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus

PROMPT_VERSION = "code:fixes_v1"  # what the investigation's prompt_version names instead


def explains(verdict: Verdict) -> bool:
    """A failing tier-1 verdict that names its failing resources, for a test with fixes."""
    return (
        verdict.tier == 1
        and verdict.status == VerdictStatus.failed
        and bool(lookup_store.failing_of(verdict))
        and bool(fixes.for_test(verdict.test_id))
    )


def _trails(db: ScopedDB, entity_id: str) -> list[dict[str, Any]]:
    row = db.fetch_one("SELECT settings FROM current_assets WHERE entity_id = %s", (entity_id,))
    settings = (row[0] if row is not None else None) or {}
    return list(settings.get("trails") or [])


def params_for(db: ScopedDB, fix: fixes.Fix, failing: dict[str, str]) -> dict[str, str] | None:
    """The fix's parameters from the failing resource; None when the resource gives none
    (an account with no trail to change)."""
    if fix.key_param == "trail_name":
        weak = [
            t
            for t in _trails(db, failing["entity_id"])
            if not (t.get("multi_region") and t.get("logging"))
        ]
        return {"trail_name": str(weak[0]["name"])} if weak else None
    return {fix.key_param: failing["name"]}


def _story(failing: dict[str, str], found: list[Lookup]) -> str:
    """What the lookups say about one resource, in a sentence or two."""
    mine = [lk for lk in found if lk.resource == failing["name"]]
    parts: list[str] = []
    for lk in mine:
        if lk.kind == "cloudtrail" and lk.status == "ok":
            last = lk.result["events"][0]
            parts.append(
                f"last changed by {last.get('username') or 'an unknown principal'} at"
                f" {last.get('event_time')} ({last.get('event_name')})"
            )
        elif lk.kind == "config_history" and lk.status == "ok":
            changes = [s for s in lk.result["states"] if s.get("changed")]
            if changes:
                parts.append(
                    f"AWS Config recorded {len(changes)} change(s), the latest at"
                    f" {changes[-1]['captured_at']}"
                )
        elif lk.kind == "infra_code" and lk.status == "ok":
            where = [f"{f['repo']}/{f['path']}" for f in lk.result.get("files", [])]
            parts.append(f"defined in code at {', '.join(where)}")
        elif lk.kind == "infra_code" and lk.status == "empty":
            parts.append("not found in the organization's Terraform")
        elif lk.status in ("skipped", "error"):
            parts.append(f"{lk.kind} not read ({lk.error})")
    return f"{failing['name']}: " + ("; ".join(parts) if parts else "no lookups recorded")


def write(db: ScopedDB, verdict: Verdict) -> Investigation:
    """Write the investigation and offer its fixes; once per verdict."""
    existing = schema.for_verdict(db, verdict.verdict_id)
    if existing is not None:
        return existing
    found = lookup_store.for_verdict(db, verdict.verdict_id)
    events: list[str] = []
    stories: list[str] = []
    offered: list[tuple[str, dict[str, str], fixes.Rendered, list[str]]] = []
    for failing in lookup_store.failing_of(verdict):
        stories.append(_story(failing, found))
        files = lookup_store.code_files(found, failing["name"])
        ids = [lk.lookup_id for lk in found if lk.resource == failing["name"]]
        for fix in fixes.for_test(verdict.test_id):
            params = params_for(db, fix, failing)
            if params is None:
                events.append(f"fix_not_applicable:{fix.fix_id}:{failing['name']}")
                continue
            try:
                rendered = fixes.render(fix, params, files)
            except fixes.Refused as exc:
                events.append(f"fix_refused:{fix.fix_id}:{exc}")
                continue
            if not rendered.passed:
                events.extend(f"fix_check_failed:{fix.fix_id}:{c.name}" for c in rendered.failed)
                continue
            offered.append((failing["name"], params, rendered, ids))
    investigation = Investigation(
        tenant_id=db.tenant.tenant_id,
        verdict_id=verdict.verdict_id,
        root_cause=f"{verdict.reasoning}. " + " ".join(f"{s}." for s in stories),
        root_cause_category="configuration",
        confidence=1.0,
        confidence_cap=1.0,
        model=None,
        prompt_version=PROMPT_VERSION,
        guardrail_events=tuple(events),
    )
    schema.persist(db, investigation)
    for resource, _params, rendered, ids in offered:
        suggestions.suggest(
            db,
            verdict_id=verdict.verdict_id,
            test_id=verdict.test_id,
            resource=resource,
            source="code",
            rendered=rendered,
            investigation_id=investigation.investigation_id,
            lookup_ids=ids,
        )
    return investigation
