"""Staleness, sending and the numbers LT3 is judged by (spec.md A-163).

An approved answer rests on verdicts and on document versions. A daily sweep marks it
stale when a verdict it relied on is no longer its subject's status or a document it
cites has a newer version, so an answer never goes out contradicting the current state.
A person sends: sending is refused while any question lacks an approved, current answer,
and what is sent is written out as the record of what left, with who approved each answer.
CCP itself transmits nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ccp.config import Settings
from ccp.evaluators import jobs
from ccp.questionnaires import store
from ccp.storage import bitemporal
from ccp.storage.scoped import ScopedDB


def sweep(db: ScopedDB, questionnaire_id: str | None = None) -> int:
    """Mark every approved answer whose verdicts or documents changed as stale."""
    heads = {(h.test_id, h.subject_key): h for h in bitemporal.heads(db)}
    rows = db.fetch_all(
        "SELECT answer_id FROM answer_status WHERE status = 'approved'"
        " AND (%s::text IS NULL OR questionnaire_id = %s)",
        (questionnaire_id, questionnaire_id),
    )
    marked = 0
    for (answer_id,) in rows:
        found = store.answer(db, answer_id)
        if found is None:
            continue
        reasons = store.stale_reasons(db, found.pointers, found.verdict_ids, heads)
        if reasons:
            store.event(db, answer_id, "stale", "ccp", "; ".join(reasons))
            marked += 1
    return marked


jobs.JOBS["answer_staleness"] = lambda db, settings, slot: sweep(db)


def send(db: ScopedDB, settings: Settings, questionnaire_id: str, by: str) -> Path:
    """Record that a person sent the questionnaire, once every answer is approved and
    still current; returns the file written as the record of what was sent."""
    found = store.get(db, questionnaire_id)
    if found is None:
        raise ValueError(f"no questionnaire {questionnaire_id!r}")
    if found["status"] == "sent":
        raise ValueError(f"questionnaire {questionnaire_id} was sent on {found['sent_at']}")
    sweep(db, questionnaire_id)
    answers = {a.question_id: a for a in store.answers_of(db, questionnaire_id)}
    waiting = [
        str(q["id"])
        for q in found["questions"]
        if str(q["id"]) not in answers or answers[str(q["id"])].status != "approved"
    ]
    if waiting:
        raise ValueError(f"not approved or no longer current: {waiting}")
    moment = datetime.now(UTC)
    record = {
        "questionnaire_id": questionnaire_id,
        "title": found["title"],
        "requester": found["requester"],
        "sent_by": by,
        "sent_at": moment.isoformat(),
        "answers": [
            {
                "question_id": a.question_id,
                "question": a.question,
                "answer": a.answer,
                "text": a.final_text,
                "approved_by": a.status_by,
                "edited": a.edited_text is not None,
                "relied_on_verdicts": a.verdict_ids,
                "cited": [
                    {"quote": p.get("quote"), "evidence": p.get("evidence_entity_id")}
                    for p in a.pointers
                ],
            }
            for a in (answers[str(q["id"])] for q in found["questions"])
        ],
    }
    out = settings.ccp_data_dir / "exports" / db.tenant.tenant_id / "questionnaires"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{questionnaire_id}.json"
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    for a in answers.values():
        store.event(db, a.answer_id, "sent", by)
    db.execute(
        "UPDATE questionnaires SET status = 'sent', sent_at = %s, sent_by = %s"
        " WHERE questionnaire_id = %s",
        (moment, by, questionnaire_id),
    )
    return path


@dataclass(frozen=True)
class Metrics:
    by_kind: dict[str, dict[str, Any]]
    stale: int
    contradicted: int
    failed_checks: int


def metrics(db: ScopedDB) -> Metrics:
    """The reviewer edit rate per kind of question (mapped to a control, read from
    evidence, or reused from the library), answers gone stale, those whose verdicts now
    say otherwise, and drafts that failed a code check."""
    rows = db.fetch_all(
        "SELECT a.answer_id, a.source, a.control_id, a.checks,"
        " bool_or(e.status = 'approved') AS approved,"
        " bool_or(e.status = 'approved' AND e.edited_text IS NOT NULL) AS edited,"
        " bool_or(e.status = 'stale') AS stale,"
        " bool_or(e.status = 'stale' AND e.note LIKE '%%verdict_changed%%') AS contradicted"
        " FROM questionnaire_answers a JOIN answer_events e"
        " ON e.tenant_id = a.tenant_id AND e.answer_id = a.answer_id"
        " GROUP BY a.answer_id, a.source, a.control_id, a.checks"
    )
    by_kind: dict[str, dict[str, Any]] = {}
    stale = contradicted = failed = 0
    for _, source, control_id, checks, approved, edited, was_stale, was_contradicted in rows:
        kind = "library" if source == "library" else ("mapped" if control_id else "read")
        bucket = by_kind.setdefault(kind, {"approved": 0, "edited": 0})
        bucket["approved"] += int(bool(approved))
        bucket["edited"] += int(bool(edited))
        stale += int(bool(was_stale))
        contradicted += int(bool(was_contradicted))
        loaded = json.loads(checks) if isinstance(checks, str) else checks
        failed += int(any(not c.get("passed") for c in loaded))
    for bucket in by_kind.values():
        bucket["edit_rate"] = bucket["edited"] / bucket["approved"] if bucket["approved"] else None
    return Metrics(by_kind, stale, contradicted, failed)
