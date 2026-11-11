"""The impact step (spec.md Layer 7 trigger): an entity change enqueues the (test, subject)
pairs it affects, coalesced per subject on a 60-second watermark.

The impact table is data (A-126): ccp/knowledge/impact.yaml, versioned with the tests it
names. Each rule gives the table whose row changed, the conditions on the change, the tests
it re-evaluates and how the subject is found; the conditions and the subjects are the closed
vocabulary below, checked at load, so a rule cannot ask for something this module cannot
answer. Only terminated people are subjects of HR-1 and GH-1, so a grant observation for an
active employee enqueues nothing for them; the termination event is what opens the subject.

Coalescing: a pending evaluation for the same (tenant, test, subject) younger than the window
absorbs the new change and carries the latest trigger_observed_at. Ten changes to one subject
inside the window enqueue one evaluation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import psycopg
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ccp.identity import resolver
from ccp.ingest import event_log
from ccp.ingest.consumers import OrderedConsumer
from ccp.ingest.event_log import LogRecord
from ccp.ingest.worker import register
from ccp.knowledge import graph as knowledge
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


IMPACT_FILE = knowledge.KNOWLEDGE_DIR / "impact.yaml"
TABLES = ("persons", "person_links", "access_grants", "assets", "evidence")
# The closed vocabulary (A-126): what a rule may ask of a change to each table, and where it
# may find the subject. A condition in LISTED takes a list of values; any other takes true.
CONDITIONS: dict[str, frozenset[str]] = {
    "persons": frozenset({"hris_person", "group_terminated"}),
    "person_links": frozenset({"group_terminated"}),
    "access_grants": frozenset({"grantee_terminated", "on_org", "asset_prefix"}),
    "assets": frozenset({"asset_type", "tagged"}),
    "evidence": frozenset({"evidence_type", "about_repo", "has_subject"}),
}
LISTED = frozenset({"asset_type", "evidence_type", "asset_prefix"})
SUBJECTS: dict[str, frozenset[str]] = {
    "persons": frozenset({"person"}),
    "person_links": frozenset({"person"}),
    "access_grants": frozenset({"grantee", "grant_org", "tenant"}),
    "assets": frozenset({"asset_repo", "asset_org", "tenant"}),
    "evidence": frozenset(
        {"evidence_repo", "evidence_repo_or_tenant", "evidence_person", "tenant"}
    ),
}
_ROW: dict[str, str] = {
    "access_grants": "SELECT person_entity_id, asset_entity_id FROM current_access_grants"
    " WHERE entity_id = %s",
    "assets": "SELECT asset_type, tags FROM current_assets WHERE entity_id = %s",
    "evidence": "SELECT evidence_type, subject_entity_id FROM current_evidence"
    " WHERE entity_id = %s",
}


class ImpactRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    table: Literal["persons", "person_links", "access_grants", "assets", "evidence"]
    when: dict[str, bool | list[str]] = Field(default_factory=dict)
    tests: tuple[str, ...] = Field(min_length=1)
    subject: str

    @model_validator(mode="after")
    def _known(self) -> ImpactRule:
        for name, value in self.when.items():
            if name not in CONDITIONS[self.table]:
                raise ValueError(f"{self.table}: unknown condition {name!r}")
            if name in LISTED and not isinstance(value, list):
                raise ValueError(f"{self.table}: {name} takes a list of values")
            if name not in LISTED and value is not True:
                raise ValueError(f"{self.table}: {name} takes true; leave it out otherwise")
        if self.subject not in SUBJECTS[self.table]:
            raise ValueError(f"{self.table}: unknown subject {self.subject!r}")
        return self


def load_table(
    path: Path = IMPACT_FILE, tests: Iterable[str] | None = None
) -> tuple[ImpactRule, ...]:
    """The impact table, every rule checked against the vocabulary and the catalogue."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = tuple(ImpactRule.model_validate(r) for r in raw["rules"])
    known = set(tests) if tests is not None else set(knowledge.load().tests)
    for rule in rules:
        unknown = set(rule.tests) - known
        if unknown:
            raise ValueError(
                f"an impact rule on {rule.table} names unknown tests {sorted(unknown)}"
            )
    return rules


IMPACT_RULES = load_table()
# What a change to each table can trigger; every test must be reachable from some table, or
# it could never run on the change path (tests/unit/test_impact_reach.py fails CI on one).
IMPACT_REACH: dict[str, frozenset[str]] = {
    table: frozenset(test for rule in IMPACT_RULES if rule.table == table for test in rule.tests)
    for table in TABLES
}


class _Change:
    """One changed row, read only as far as the rules ask."""

    def __init__(self, db: ScopedDB, table: str, entity_id: str) -> None:
        self.db, self.table, self.entity_id = db, table, entity_id
        self._row: tuple[Any, ...] | None = None
        self._read = False

    def row(self) -> tuple[Any, ...] | None:
        if not self._read:
            self._read = True
            query = _ROW.get(self.table)
            found = self.db.fetch_one(query, (self.entity_id,)) if query else None
            self._row = tuple(found) if found else None
        return self._row

    def _canonical(self, entity_id: str) -> str:
        return resolver.canonical_id(self.db, entity_id)

    def _repo(self) -> str | None:
        row = self.row()
        subject = str(row[1]) if row and row[1] else None
        if subject and subject.startswith("github:asset:R_"):
            return f"repo:{subject}"
        return None

    def holds(self, name: str, value: bool | list[str]) -> bool:
        if name == "hris_person":
            return self.entity_id.startswith("hris:person:")
        if name == "group_terminated":
            return _group_is_terminated(self.db, self._canonical(self.entity_id))
        row = self.row()
        if row is None:  # the other conditions read the row; a row that is gone matches none
            return False
        if name == "grantee_terminated":
            return _group_is_terminated(self.db, self._canonical(str(row[0])))
        if name == "on_org":
            return str(row[1]).startswith(ORG_ASSET_PREFIX)
        if name == "asset_prefix":  # a grant on an AWS account or an IdP (A-140)
            return isinstance(value, list) and str(row[1]).startswith(tuple(value))
        if name in ("asset_type", "evidence_type"):
            return isinstance(value, list) and str(row[0]) in value
        if name == "tagged":
            tags = json.loads(row[1]) if isinstance(row[1], str) else row[1]
            return bool(tags)
        if name == "about_repo":
            return self._repo() is not None
        if name == "has_subject":
            return bool(row[1])
        raise ValueError(f"unknown condition {name!r}")  # pragma: no cover - checked at load

    def subject(self, name: str) -> str:
        if name == "tenant":
            return "tenant"
        if name == "person":
            return f"person:{self._canonical(self.entity_id)}"
        row = self.row()
        assert row is not None  # a rule's conditions read the row before its subject
        if name == "grantee":
            return f"person:{self._canonical(str(row[0]))}"
        if name == "grant_org":
            return f"org:{row[1]}"
        if name == "asset_repo":
            return f"repo:{self.entity_id}"
        if name == "asset_org":
            return f"org:{self.entity_id}"
        if name == "evidence_person":
            return f"person:{self._canonical(str(row[1]))}"
        repo = self._repo()
        if name == "evidence_repo" and repo is not None:
            return repo
        if name == "evidence_repo_or_tenant":
            return repo or "tenant"
        raise ValueError(f"no subject {name!r} for this change")  # pragma: no cover


def requests_for_entity(
    db: ScopedDB, table: str, entity_id: str, observed_at: datetime
) -> list[EvaluationRequest]:
    """The impact table (knowledge/impact.yaml) applied to one change (A-126)."""
    change = _Change(db, table, entity_id)
    out: list[EvaluationRequest] = []
    for rule in IMPACT_RULES:
        if rule.table != table:
            continue
        if all(change.holds(name, value) for name, value in rule.when.items()):
            subject = change.subject(rule.subject)
            out.extend(EvaluationRequest(test, subject, observed_at) for test in rule.tests)
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
