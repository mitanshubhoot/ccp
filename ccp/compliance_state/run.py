"""`ccp run --tenant`: the whole pipeline for one tenant, in order, idempotently.

    ingest (fixtures) -> normalize -> identity resolve -> index refresh
    -> enqueue every (test, subject) pair -> drain evaluations -> write compliance state

Re-running is safe at every stage: landing dedups, entity inserts are keyed, links only
grow, the index is content-addressed, verdicts are keyed, and state is idempotent on
verdict id. AI tiers run when a model key is set and park otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ccp.agent import tier3 as _tier3  # noqa: F401 - registers the tier 3 evaluator
from ccp.compliance_state import reader, writer
from ccp.config import Settings
from ccp.connectors.base import SourceKind
from ccp.evaluators import runner as evaluations
from ccp.evaluators.tier2 import Providers
from ccp.identity import resolver
from ccp.ingest.landing import ingest_tenant
from ccp.normalize.consumer import normalize_all
from ccp.observability import steps
from ccp.retrieval import index
from ccp.storage.scoped import ScopedDB


@dataclass
class RunReport:
    tenant_id: str
    landed: int = 0
    duplicates: int = 0
    normalized: int = 0
    identity_compared: int = 0
    chunks: int = 0
    enqueued: int = 0
    evaluated: int = 0
    verdict_duplicates: int = 0
    parked: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    state_written: int = 0
    state_by_reason: dict[str, int] = field(default_factory=dict)
    ai_tiers_available: bool = False
    heads: dict[str, int] = field(default_factory=dict)


def run(
    db: ScopedDB,
    settings: Settings,
    *,
    providers: Providers | None = None,
    source: SourceKind = "fixture",
    now: datetime | None = None,
    ingest: bool = True,
) -> RunReport:
    report = RunReport(tenant_id=db.tenant.tenant_id)
    active = providers or Providers.from_settings(settings)
    report.ai_tiers_available = active.llm is not None
    with steps.step("run", layer="compliance_state", tenant=db.tenant, inputs={"source": source}):
        if ingest:
            landing = ingest_tenant(settings, db, db.tenant, source=source)
            report.landed, report.duplicates = landing.landed, landing.duplicates
        report.normalized = normalize_all(db, settings)
        report.identity_compared = resolver.resolve_tenant(db).compared
        report.chunks = index.refresh(db, settings, active.embedder, active.llm).chunks
        report.enqueued = evaluations.enqueue_all(db, settings, now=now)
        summary = evaluations.drain(db, settings, now=now, providers=active)
        report.evaluated = summary.evaluated
        report.verdict_duplicates = summary.duplicates
        report.by_status = dict(sorted(summary.by_status.items()))
        report.parked = evaluations.parked_count(db)
        written = writer.drain(db)
        report.state_written = written.written
        report.state_by_reason = dict(sorted(written.by_reason.items()))
        report.heads = reader.status_counts(db).by_status
    return report


def format_report(report: RunReport) -> str:
    lines = [
        f"tenant {report.tenant_id}",
        f"ingest      landed {report.landed}, duplicates {report.duplicates}",
        f"normalize   {report.normalized} raw records",
        f"identity    {report.identity_compared} new comparisons",
        f"index       {report.chunks} chunks",
        f"evaluate    enqueued {report.enqueued}, evaluated {report.evaluated}"
        f" (duplicates {report.verdict_duplicates}), parked {report.parked}",
    ]
    for status, n in report.by_status.items():
        lines.append(f"            {status:<13} {n}")
    if not report.ai_tiers_available:
        lines.append("            AI tiers parked: ANTHROPIC_API_KEY is not set")
    lines.append(f"state       {report.state_written} rows written {report.state_by_reason}")
    lines.append(f"heads       {report.heads or 'NO EVALUATIONS YET'}")
    return "\n".join(lines)
