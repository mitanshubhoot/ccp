"""Whether an uploaded document counts, by code's admissibility checks (spec.md A-161).

The same checks feed the uploader's feedback and the index: a model-transcribed document
(a scan or a screenshot) with a blocking reason is left out of what the evaluators search
until a new upload fixes it. The manifest entry and front matter come from the raw event
the upload landed with.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ccp.config import Settings
from ccp.ingest.raw_zone import read_ref
from ccp.knowledge import admissibility
from ccp.knowledge.admissibility import Admissibility
from ccp.normalize.entities import Evidence
from ccp.storage.scoped import ScopedDB

UPLOADS = ("manual",)


def raw_context(
    db: ScopedDB, evidence: Evidence, settings: Settings | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The manifest entry and front matter the upload landed with, from its raw event."""
    row = db.fetch_one(
        "SELECT raw_path FROM raw_events WHERE connector = %s AND source_event_id = %s",
        (evidence.source_connector, evidence.source_event_id),
    )
    if row is None:
        return {}, {}
    try:
        payload = read_ref(str(row[0]), settings).payload
    except (OSError, ValueError, KeyError):
        return {}, {}
    return dict(payload.get("manifest") or {}), dict(payload.get("front_matter") or {})


def assess(
    db: ScopedDB, evidence: Evidence, settings: Settings | None, now: datetime
) -> Admissibility:
    manifest, front_matter = raw_context(db, evidence, settings)
    return admissibility.check(
        evidence_type=evidence.evidence_type.value,
        source_connector=evidence.source_connector,
        issued_at=evidence.issued_at,
        text=evidence.text,
        page_methods=evidence.page_methods,
        subject_entity_id=evidence.subject_entity_id,
        manifest=manifest,
        front_matter=front_matter,
        now=now,
        image=admissibility.transcribed(evidence.page_methods) and not evidence.page_texts,
    )


def counted(db: ScopedDB, evidence: Evidence, settings: Settings | None, now: datetime) -> bool:
    """False only for an uploaded, model-transcribed document with a blocking reason."""
    if evidence.source_connector not in UPLOADS:
        return True
    if not admissibility.transcribed(evidence.page_methods):
        return True
    return assess(db, evidence, settings, now).admissible
