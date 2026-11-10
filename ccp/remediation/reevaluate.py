"""Re-evaluation after a proposal executed or was simulated (spec.md Layer 10 "Re-evaluate").

Nothing here calls an evaluator directly: the affected connector is re-pulled, the new
observations land like any other change, the impact step enqueues the tests, the
evaluations consumer produces the verdicts, and the state writer records them. The loop
closes through the same topic every other change uses.
"""

from __future__ import annotations

from dataclasses import dataclass

from ccp.compliance_state import writer
from ccp.compliance_state.run import prepare_changes
from ccp.config import Settings
from ccp.connectors.base import Connector
from ccp.evaluators import runner as evaluations
from ccp.evaluators.impact import ImpactConsumer
from ccp.evaluators.tier2 import Providers
from ccp.ingest.landing import sync_connector
from ccp.normalize.consumer import normalize_all
from ccp.observability import steps
from ccp.storage.scoped import ScopedDB


@dataclass(frozen=True)
class ReevaluationSummary:
    landed: int
    normalized: int
    enqueued: int
    evaluated: int
    state_written: int
    pull_status: str = "complete"  # the re-pull is a sync run like any other (A-118)


def reevaluate(
    db: ScopedDB, settings: Settings, connector: Connector, providers: Providers | None = None
) -> ReevaluationSummary:
    with steps.step("remediate.reevaluate", layer="remediation", tenant=db.tenant) as s:
        pulled = sync_connector(settings, db, db.tenant, connector)
        landed = pulled.landed
        normalized = normalize_all(db, settings)
        prepare_changes(db, settings, providers or Providers.from_settings(settings))
        impact = ImpactConsumer(db)
        while impact.run_once():
            pass
        evaluated = evaluations.drain(db, settings, providers=providers)
        written = writer.drain(db)
        summary = ReevaluationSummary(
            landed=landed,
            normalized=normalized,
            enqueued=impact.enqueued,
            evaluated=evaluated.evaluated,
            state_written=written.written,
            pull_status=pulled.status,
        )
        s.set_output(summary.__dict__)
    return summary
