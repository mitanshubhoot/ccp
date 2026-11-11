"""Shadow diffs over stored artifacts (spec.md A-143).

A candidate bundle (a new model for tier 2, say) is run against the requests CCP already
sent, never against live traffic and never writing a verdict: each stored tier-2 request is
re-sent, as recorded, to the candidate model, and the test's own rule is applied to the
stored fields and to the candidate's. A status that would change is a flip; a flip towards
PASS is the one that matters most, because a false PASS is the costly error (6.6). Each
run is recorded as a quality check (kind shadow) with its flips, and fails when any flip
reaches PASS, so a regression injected in shadow shows the next night (MT7's metric).

The candidate's quotes are not re-verified here: the comparison is rule against rule on the
fields each side returned, which is what a change of model changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ulid import ULID

from ccp.config import Settings
from ccp.evaluators import rules
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.extraction import schema_model
from ccp.knowledge import graph as knowledge
from ccp.llm import release
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import LLMClient, SystemBlock
from ccp.replay import rebuild as replay
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus


@dataclass(frozen=True)
class Flip:
    verdict_id: str
    test_id: str
    subject_key: str
    stored: str
    candidate: str
    fields: tuple[str, ...]


@dataclass
class ShadowReport:
    candidate_model: str
    compared: int = 0
    skipped: int = 0
    flips: list[Flip] = field(default_factory=list)

    @property
    def towards_pass(self) -> list[Flip]:
        return [f for f in self.flips if f.candidate == VerdictStatus.passed.value]

    @property
    def passed(self) -> bool:
        return not self.towards_pass


def _values(fields: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        out[key] = value.get("value") if isinstance(value, dict) and "value" in value else value
    return out


def run(
    db: ScopedDB,
    settings: Settings,
    llm: LLMClient,
    candidate_model: str,
    *,
    tests: tuple[str, ...] = (),
    limit: int = 50,
    record: bool = True,
) -> ShadowReport:
    graph = knowledge.load()
    report = ShadowReport(candidate_model)
    ids = db.fetch_all(
        "SELECT verdict_id FROM verdicts WHERE tier = 2 AND request_json IS NOT NULL"
        + (" AND test_id = ANY(%s)" if tests else "")
        + " ORDER BY created_at DESC, verdict_id DESC LIMIT %s",
        ((list(tests), limit) if tests else (limit,)),
    )
    for (verdict_id,) in ids:
        stored = verdicts.load(db, str(verdict_id))
        if stored is None:
            continue
        spec = graph.tests[stored.test_id]
        call = next(
            (c for c in replay.rebuild(db, settings, stored.verdict_id).calls if c.schema), None
        )
        user, documents = replay.request_parts(call.request) if call else (None, [])
        model_cls = schema_model(call.schema) if call and call.schema else None
        if call is None or model_cls is None or user is None:
            report.skipped += 1
            continue
        system = [
            SystemBlock(text, cache=cached) for text, cached in replay.system_texts(call.request)
        ]
        extraction = llm.extract(
            schema=model_cls,
            system=system,
            user=user,
            model=candidate_model,
            tracker=BudgetTracker(RunBudget.from_settings(settings)),
            documents=documents,
        )
        params = dict(spec.parameters)
        params.setdefault("evidence_max_age_days", spec.evidence_max_age_days)
        before_values = _values(stored.extracted_fields)
        after_values = _values(extraction.parsed.model_dump(mode="json"))
        before = rules.decide(spec.rule, before_values, params, stored.created_at).status
        after = rules.decide(spec.rule, after_values, params, stored.created_at).status
        report.compared += 1
        if before != after:
            changed = tuple(
                k
                for k in sorted(set(before_values) | set(after_values))
                if before_values.get(k) != after_values.get(k)
            )
            report.flips.append(
                Flip(
                    stored.verdict_id,
                    stored.test_id,
                    stored.subject_key,
                    before.value,
                    after.value,
                    changed,
                )
            )
    if record:
        db.execute(
            "INSERT INTO quality_checks (tenant_id, check_id, kind, bundle_id, passed, measured)"
            " VALUES (%s, %s, 'shadow', %s, %s, %s)",
            (
                db.tenant.tenant_id,
                str(ULID()),
                release.bundle_id(settings.model_copy(update={"ccp_tier2_model": candidate_model})),
                report.passed,
                json.dumps(
                    {
                        "candidate_model": candidate_model,
                        "compared": report.compared,
                        "skipped": report.skipped,
                        "flips": [f.__dict__ for f in report.flips],
                    },
                    default=list,
                ),
            ),
        )
    return report
