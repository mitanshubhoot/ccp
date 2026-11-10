"""The impact step (spec.md Layer 7 trigger): an entity change enqueues the (test, subject)
pairs it affects, coalesced per subject on a 60-second watermark.

| Entity change                                       | Tests          | Subject          |
|-----------------------------------------------------|----------------|------------------|
| Person (HRIS) whose canonical group is terminated   | HR-1, GH-1     | canonical person |
| person_links new merge onto a terminated person     | HR-1, GH-1     | canonical person |
| AccessGrant (any connector), grantee terminated     | HR-1, GH-1     | canonical person |
| AccessGrant on the org asset                        | GH-2           | that org         |
| Asset repo (settings)                               | GH-3           | that repo        |
| Asset org                                           | GH-2           | that org         |
| Asset with tags                                     | HR-4           | tenant           |
| Evidence policy_document                            | GH-4           | repo or tenant   |
| Evidence of any type whose subject is a repo        | GH-5           | that repo        |
| Evidence identity_mfa_status                        | HR-2           | tenant           |
| Evidence training_record                            | HR-3           | subject person   |
| Evidence access_review_report, critical_systems_list| HR-4           | tenant           |
| Evidence account_registry                           | HR-5           | tenant           |

Only terminated people are subjects of HR-1 and GH-1, so a grant observation for an active
employee enqueues nothing for them; the termination event is what opens the subject.

Coalescing: a pending evaluation for the same (tenant, test, subject) younger than the window
absorbs the new change and carries the latest trigger_observed_at. Ten changes to one subject
inside the window enqueue one evaluation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from ccp.identity import resolver
from ccp.ingest import event_log
from ccp.ingest.consumers import OrderedConsumer
from ccp.ingest.event_log import LogRecord
from ccp.ingest.worker import register
from ccp.observability import steps
from ccp.storage.scoped import ScopedDB

COALESCE_SECONDS = 60
ORG_ASSET_PREFIX = "github:asset:org:"


@dataclass(frozen=True)
class EvaluationRequest:
    test_id: str
    subject: str  # scope without the test prefix, e.g. "person:hris:person:E1042" or "tenant"
    trigger_observed_at: datetime
    reason: str = "impact"

    @property
    def partition_key(self) -> str:
        return f"{self.test_id}|{self.subject}"

    def to_ref(self) -> str:
        return json.dumps(
            {
                "test_id": self.test_id,
                "subject": self.subject,
                "trigger_observed_at": self.trigger_observed_at.isoformat(),
                "reason": self.reason,
            }
        )

    @classmethod
    def from_ref(cls, ref: str) -> EvaluationRequest:
        data = json.loads(ref)
        return cls(
            test_id=data["test_id"],
            subject=data["subject"],
            trigger_observed_at=datetime.fromisoformat(data["trigger_observed_at"]),
            reason=data.get("reason", "impact"),
        )


def enqueue(
    cur: psycopg.Cursor[Any],
    tenant_id: str,
    request: EvaluationRequest,
    coalesce_seconds: int = COALESCE_SECONDS,
) -> bool:
    """Append an evaluation, or fold it into a pending one for the same (test, subject)
    produced inside the coalescing window. Returns True when a record was appended."""
    key = f"{tenant_id}:{request.partition_key}"
    if coalesce_seconds > 0:
        pending = cur.execute(
            'SELECT "offset", source_ts FROM event_log WHERE topic = %s AND status = %s'
            " AND partition_key = %s AND produced_at > now() - make_interval(secs => %s)"
            ' ORDER BY "offset" DESC LIMIT 1 FOR UPDATE',
            ("evaluations", "pending", key, coalesce_seconds),
        ).fetchone()
        if pending is not None:
            offset, source_ts = int(pending[0]), pending[1]
            if request.trigger_observed_at > source_ts:  # carry the latest trigger
                cur.execute(
                    'UPDATE event_log SET source_ts = %s, ref = %s WHERE "offset" = %s',
                    (request.trigger_observed_at, request.to_ref(), offset),
                )
            return False
    event_log.append(
        cur,
        tenant_id=tenant_id,
        topic="evaluations",
        partition_key=key,
        source_ts=request.trigger_observed_at,
        ref=request.to_ref(),
    )
    return True


def _group(db: ScopedDB, canonical: str) -> list[str]:
    rows = db.fetch_all(
        "SELECT person_entity_id FROM current_person_links WHERE canonical_person_id = %s",
        (canonical,),
    )
    return sorted({canonical, *(str(r[0]) for r in rows)})


def _group_is_terminated(db: ScopedDB, canonical: str) -> bool:
    """True when an HRIS person in the canonical group is terminated in its newest known
    status (an 'unknown' partial observation never overrides an earlier known one)."""
    for entity_id in _group(db, canonical):
        if not entity_id.startswith("hris:person:"):
            continue
        row = db.fetch_one(
            "SELECT employment_status FROM persons WHERE entity_id = %s"
            " AND employment_status <> 'unknown' ORDER BY observed_at DESC, source_event_id DESC"
            " LIMIT 1",
            (entity_id,),
        )
        if row and row[0] == "terminated":
            return True
    return False


def _person_tests(db: ScopedDB, entity_id: str, trigger: datetime) -> list[EvaluationRequest]:
    canonical = resolver.canonical_id(db, entity_id)
    if not _group_is_terminated(db, canonical):
        return []
    subject = f"person:{canonical}"
    return [EvaluationRequest(t, subject, trigger) for t in ("HR-1", "GH-1")]


# What a change to each table can trigger (A-101). `requests_for_entity` emits only these,
# and every test must be reachable from some table, or it could never run on the change
# path; tests/unit/test_impact_reach.py fails CI on a test with no trigger.
IMPACT_REACH: dict[str, frozenset[str]] = {
    "persons": frozenset({"HR-1", "GH-1"}),
    "person_links": frozenset({"HR-1", "GH-1"}),
    "access_grants": frozenset({"HR-1", "GH-1", "GH-2"}),
    "assets": frozenset({"GH-2", "GH-3", "HR-4"}),
    "evidence": frozenset({"GH-4", "GH-5", "HR-2", "HR-3", "HR-4", "HR-5"}),
}


def requests_for_entity(
    db: ScopedDB, table: str, entity_id: str, observed_at: datetime
) -> list[EvaluationRequest]:
    """The impact table, as code. Reads only what it needs to classify the change."""
    out: list[EvaluationRequest] = []
    if table == "persons":
        if entity_id.startswith("hris:person:"):
            out += _person_tests(db, entity_id, observed_at)
    elif table == "person_links":
        out += _person_tests(db, entity_id, observed_at)
    elif table == "access_grants":
        row = db.fetch_one(
            "SELECT person_entity_id, asset_entity_id FROM current_access_grants"
            " WHERE entity_id = %s",
            (entity_id,),
        )
        if row:
            person, asset = str(row[0]), str(row[1])
            out += _person_tests(db, person, observed_at)
            if asset.startswith(ORG_ASSET_PREFIX):
                out.append(EvaluationRequest("GH-2", f"org:{asset}", observed_at))
    elif table == "assets":
        row = db.fetch_one(
            "SELECT asset_type, tags FROM current_assets WHERE entity_id = %s", (entity_id,)
        )
        if row:
            asset_type = str(row[0])
            if asset_type == "repo":
                out.append(EvaluationRequest("GH-3", f"repo:{entity_id}", observed_at))
            elif asset_type == "org":
                out.append(EvaluationRequest("GH-2", f"org:{entity_id}", observed_at))
            tags = json.loads(row[1]) if isinstance(row[1], str) else row[1]
            if tags:
                out.append(EvaluationRequest("HR-4", "tenant", observed_at))
    elif table == "evidence":
        row = db.fetch_one(
            "SELECT evidence_type, subject_entity_id FROM current_evidence WHERE entity_id = %s",
            (entity_id,),
        )
        if row:
            evidence_type = str(row[0])
            subject_entity = str(row[1]) if row[1] else None
            repo_subject = (
                f"repo:{subject_entity}"
                if subject_entity and subject_entity.startswith("github:asset:R_")
                else None
            )
            if evidence_type == "policy_document":
                out.append(EvaluationRequest("GH-4", repo_subject or "tenant", observed_at))
            if repo_subject:
                out.append(EvaluationRequest("GH-5", repo_subject, observed_at))
            if evidence_type == "identity_mfa_status":
                out.append(EvaluationRequest("HR-2", "tenant", observed_at))
            elif evidence_type == "training_record" and subject_entity:
                canonical = resolver.canonical_id(db, subject_entity)
                out.append(EvaluationRequest("HR-3", f"person:{canonical}", observed_at))
            elif evidence_type in ("access_review_report", "critical_systems_list"):
                out.append(EvaluationRequest("HR-4", "tenant", observed_at))
            elif evidence_type == "account_registry":
                out.append(EvaluationRequest("HR-5", "tenant", observed_at))
    return out


def parse_entity_ref(record: LogRecord) -> tuple[str, str]:
    """(table, entity_id) from an entities-topic record. The ref is
    ``{table}:{entity_id}:{source_event_id}``; entity and event ids contain colons, so the
    entity id is taken from the partition key ``{tenant_id}:{entity_id}`` instead."""
    table = record.ref.split(":", 1)[0]
    entity_id = record.partition_key.split(":", 1)[1]
    return table, entity_id


class ImpactConsumer(OrderedConsumer):
    """entities topic -> evaluations topic."""

    name = "impact"
    topic = "entities"

    def __init__(self, db: ScopedDB, coalesce_seconds: int = COALESCE_SECONDS) -> None:
        super().__init__(db)
        self.coalesce_seconds = coalesce_seconds
        self.enqueued = 0
        self.coalesced = 0

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        table, entity_id = parse_entity_ref(record)
        with steps.step(
            "impact.map",
            layer="evaluators",
            tenant=self._db.tenant,
            inputs={"offset": record.offset, "table": table, "entity_id": entity_id},
        ) as s:
            appended = 0
            requests = requests_for_entity(self._db, table, entity_id, record.source_ts)
            for request in requests:
                if enqueue(cur, self._db.tenant.tenant_id, request, self.coalesce_seconds):
                    appended += 1
                else:
                    self.coalesced += 1
            self.enqueued += appended
            s.set_output({"requests": len(requests), "enqueued": appended})


register("impact")(lambda db, settings: ImpactConsumer(db))
