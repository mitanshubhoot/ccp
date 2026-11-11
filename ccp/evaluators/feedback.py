"""Feedback on an upload, seconds after it lands (spec.md A-161).

A consumer of the entities topic: for every new version of an uploaded document it stores
the document's segments (the ids a model will cite) and code's admissibility checks, with
the provenance badge, as an `evidence_feedback` row the uploader sees at once. No model is
called here; the adequacy verifier (A-162) adds its own row later, from its own job.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import psycopg
from ulid import ULID

from ccp.config import Settings
from ccp.ingest.consumers import OrderedConsumer
from ccp.ingest.event_log import LogRecord
from ccp.ingest.worker import register
from ccp.normalize.entities import Evidence
from ccp.normalize.repository import _EVIDENCE_COLUMNS, _evidence
from ccp.retrieval import segments
from ccp.retrieval.admission import UPLOADS, assess
from ccp.storage.scoped import ScopedDB


def record(
    db: ScopedDB,
    evidence: Evidence,
    *,
    kind: str,
    admissible: bool | None,
    badge: str,
    reasons: list[dict[str, Any]],
    detail: dict[str, Any] | None = None,
    shown: bool = True,
    seconds: float | None = None,
) -> str:
    feedback_id = str(ULID())
    digest = segments.text_sha256(segments.pages_of(evidence.text, evidence.page_texts))
    db.execute(
        "INSERT INTO evidence_feedback (tenant_id, feedback_id, evidence_entity_id,"
        " source_event_id, text_sha256, kind, admissible, badge, reasons, detail, shown,"
        " seconds_after_landing) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            feedback_id,
            evidence.entity_id,
            evidence.source_event_id,
            digest,
            kind,
            admissible,
            badge,
            json.dumps(reasons),
            json.dumps(detail or {}),
            shown,
            seconds,
        ),
    )
    return feedback_id


def give(
    db: ScopedDB, settings: Settings | None, evidence: Evidence, landed_at: datetime | None = None
) -> str:
    """Segments and admissibility for one document version, recorded once."""
    seen = db.fetch_one(
        "SELECT feedback_id FROM evidence_feedback WHERE evidence_entity_id = %s"
        " AND source_event_id = %s AND kind = 'admissibility'",
        (evidence.entity_id, evidence.source_event_id),
    )
    if seen is not None:
        return str(seen[0])
    found = segments.ensure(db, evidence.entity_id, evidence.text, evidence.page_texts)
    now = datetime.now(UTC)
    result = assess(evidence, settings, now)
    return record(
        db,
        evidence,
        kind="admissibility",
        admissible=result.admissible,
        badge=result.badge,
        reasons=[r.__dict__ for r in result.reasons],
        detail={"segments": len(found.segments), "segmenter": found.version},
        seconds=(now - landed_at).total_seconds() if landed_at else None,
    )


def latest(db: ScopedDB, evidence_entity_id: str, shown_only: bool = True) -> list[dict[str, Any]]:
    """The newest feedback of each kind for an evidence entity, as the uploader sees it."""
    rows = db.fetch_all(
        "SELECT DISTINCT ON (kind) kind, admissible, badge, reasons, detail, shown,"
        " seconds_after_landing, source_event_id, created_at FROM evidence_feedback"
        " WHERE evidence_entity_id = %s AND (shown OR NOT %s)"
        " ORDER BY kind, created_at DESC, feedback_id DESC",
        (evidence_entity_id, shown_only),
    )
    return [
        {
            "kind": r[0],
            "admissible": r[1],
            "badge": r[2],
            "reasons": json.loads(r[3]) if isinstance(r[3], str) else r[3],
            "detail": json.loads(r[4]) if isinstance(r[4], str) else r[4],
            "shown": r[5],
            "seconds_after_landing": r[6],
            "source_event_id": r[7],
            "created_at": r[8],
        }
        for r in rows
    ]


class EvidenceFeedback(OrderedConsumer):
    """A consumer of the entities topic: feedback on every new version of an upload."""

    name = "evidence-feedback"
    topic = "entities"

    def __init__(self, db: ScopedDB, settings: Settings | None = None) -> None:
        super().__init__(db)
        self.settings = settings
        self.given = 0

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        if not record.ref.startswith("evidence:"):
            return
        row = cur.execute(
            f"SELECT {_EVIDENCE_COLUMNS} FROM evidence"  # noqa: S608
            " WHERE 'evidence:' || entity_id || ':' || source_event_id = %s LIMIT 1",
            (record.ref,),
        ).fetchone()
        if row is None:
            return
        evidence = _evidence(row)
        if evidence.source_connector not in UPLOADS:
            return
        give(self._db, self.settings, evidence, record.produced_at)
        self.given += 1


register("evidence-feedback")(lambda db, settings: EvidenceFeedback(db, settings))


def drain(db: ScopedDB, settings: Settings | None = None) -> int:
    consumer = EvidenceFeedback(db, settings)
    while consumer.run_once():
        pass
    return consumer.given
