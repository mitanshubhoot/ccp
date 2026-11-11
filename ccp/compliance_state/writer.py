"""The state writer (spec.md Layer 9): consumes the `verdicts` topic and writes exactly one
compliance_state row per verdict through the bitemporal writer.

    the verdict                         compliance_state.reason
    trigger human_decision              human_override
    trigger correction                  correction    supersedes, at its own valid time
    decided under a rule stamp other    rule_change   valid no earlier than the new stamp's
      than the head verdict's                         first verdict: an amendment (A-128)
    trigger stale_evidence or           stale_evidence
      revalidate_interval
    valid_from before the head's        late_event    a fact about the past arrived late
    anything else                       evaluation

Idempotent on verdict_id: re-consuming the topic writes nothing. A needs_review verdict is
written as a fact ("as of now, we do not know"); the human's later decision supersedes it.
A verdict about an earlier moment than the subject's head is filed into history, bounded at
the next newer fact, and never displaces the newer head (A-107).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg

from ccp.evaluators import rule_changes
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.verdict import Verdict
from ccp.ingest.consumers import OrderedConsumer
from ccp.ingest.event_log import LogRecord
from ccp.ingest.worker import register
from ccp.knowledge import catalogue
from ccp.knowledge import graph as knowledge
from ccp.knowledge.graph import KnowledgeGraph
from ccp.observability import steps
from ccp.storage import bitemporal
from ccp.storage.bitemporal import Reason, StateRow
from ccp.storage.scoped import ScopedDB


@dataclass
class WriteSummary:
    consumed: int = 0
    written: int = 0
    skipped_existing: int = 0
    missing_verdicts: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)


def reason_for(verdict: Verdict, head: StateRow | None, head_stamp: str | None = None) -> Reason:
    if verdict.trigger_reason == "human_decision":
        return "human_override"
    if verdict.trigger_reason == "correction":
        return "correction"
    if head is not None and head_stamp and verdict.rule_version not in (None, head_stamp):
        return "rule_change"  # a rule changed under the subject: an amendment (A-128)
    if verdict.trigger_reason in ("stale_evidence", "revalidate_interval"):
        return "stale_evidence"
    if head is not None and verdict.valid_from < head.valid_from:
        return "late_event"
    return "evaluation"


def already_written(db: ScopedDB, verdict_id: str) -> bool:
    row = db.fetch_one("SELECT 1 FROM compliance_state WHERE verdict_id = %s", (verdict_id,))
    return row is not None


def write_verdict(
    db: ScopedDB, verdict: Verdict, graph: KnowledgeGraph | None = None
) -> StateRow | None:
    """One state row for one verdict, or None when it was written before."""
    if already_written(db, verdict.verdict_id):
        return None
    graph = graph or knowledge.load()
    control_ids = tuple(c.control_id for c in graph.controls_for_test(verdict.test_id))
    head = bitemporal.current_state(db, verdict.subject_key)
    head_verdict = verdicts.load(db, head.verdict_id) if head is not None else None
    head_stamp = head_verdict.rule_version if head_verdict is not None else None
    reason = reason_for(verdict, head, head_stamp)
    valid_from = verdict.valid_from
    if reason == "rule_change" and verdict.rule_version and head_stamp:
        first = _first_use(db, verdict.test_id, verdict.rule_version) or verdict.created_at
        valid_from = max(valid_from, first)  # the past stands: an amendment (A-128)
        with db.transaction() as cur:
            rule_changes.note_amendment(
                cur, db.tenant.tenant_id, verdict.test_id, head_stamp, verdict.rule_version, first
            )
    return bitemporal.record(
        db,
        subject_key=verdict.subject_key,
        test_id=verdict.test_id,
        control_ids=control_ids,
        status=verdict.status,
        verdict_id=verdict.verdict_id,
        valid_from=valid_from,
        reason=reason,
        # an older fact never displaces a newer head (A-107); a correction may (A-128)
        late_into_history=reason != "correction",
        catalogue_version=catalogue.version(),
    )


def _first_use(db: ScopedDB, test_id: str, stamp: str) -> datetime | None:
    """When a rule stamp first decided anything of this test for the tenant."""
    row = db.fetch_one(
        "SELECT min(created_at) FROM verdicts WHERE test_id = %s AND rule_version = %s",
        (test_id, stamp),
    )
    return row[0] if row and row[0] is not None else None


class StateWriterConsumer(OrderedConsumer):
    name = "state"
    topic = "verdicts"

    def __init__(self, db: ScopedDB) -> None:
        super().__init__(db)
        self.graph = knowledge.load()
        self.summary = WriteSummary()

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        self.summary.consumed += 1
        with steps.step(
            "state.write",
            layer="compliance_state",
            tenant=self._db.tenant,
            inputs={"offset": record.offset, "verdict_id": record.ref},
        ) as s:
            verdict = verdicts.load(self._db, record.ref)
            if verdict is None:
                self.summary.missing_verdicts += 1
                s.event(f"verdict_missing:{record.ref}")
                s.set_output({"written": False})
                return
            row = write_verdict(self._db, verdict, self.graph)
            if row is None:
                self.summary.skipped_existing += 1
                s.set_output({"written": False, "reason": "already_written"})
                return
            self.summary.written += 1
            self.summary.by_reason[row.reason] = self.summary.by_reason.get(row.reason, 0) + 1
            s.set_output(
                {
                    "written": True,
                    "row_id": row.row_id,
                    "status": row.status.value,
                    "reason": row.reason,
                    "supersedes": row.supersedes_row_id,
                }
            )


register("state")(lambda db, settings: StateWriterConsumer(db))


def drain(db: ScopedDB) -> WriteSummary:
    consumer = StateWriterConsumer(db)
    while consumer.run_once():
        pass
    return consumer.summary
