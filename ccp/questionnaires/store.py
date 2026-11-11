"""Questionnaires, their drafted answers, the approved-answer library, and what makes an
answer stale (spec.md A-163, A-164)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ulid import ULID

from ccp.evaluators import verdict as verdicts
from ccp.normalize.repository import evidence_version
from ccp.storage import bitemporal
from ccp.storage.scoped import ScopedDB

Status = Literal["draft", "approved", "rejected", "stale", "sent"]
MATCH_JACCARD = 0.8
_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def question_key(text: str) -> str:
    """The same question, however it is spaced or punctuated, has the same key."""
    return hashlib.sha256(" ".join(_WORD.findall(text.lower())).encode()).hexdigest()[:16]


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


# -- questionnaires ------------------------------------------------------------------------


def create(db: ScopedDB, title: str, questions: list[dict[str, Any]], requester: str = "") -> str:
    ids = [str(q.get("id") or "") for q in questions]
    if not questions or any(not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("every question needs its own id")
    questionnaire_id = str(ULID())
    db.execute(
        "INSERT INTO questionnaires (tenant_id, questionnaire_id, title, requester, questions)"
        " VALUES (%s, %s, %s, %s, %s)",
        (db.tenant.tenant_id, questionnaire_id, title, requester, json.dumps(questions)),
    )
    return questionnaire_id


def get(db: ScopedDB, questionnaire_id: str) -> dict[str, Any] | None:
    row = db.fetch_one(
        "SELECT questionnaire_id, title, requester, questions, status, created_at, sent_at,"
        " sent_by FROM questionnaires WHERE questionnaire_id = %s",
        (questionnaire_id,),
    )
    if row is None:
        return None
    keys = ("questionnaire_id", "title", "requester", "questions", "status", "created_at")
    out = dict(zip(keys, row[:6], strict=True))
    out["questions"] = _load(out["questions"])
    out["sent_at"], out["sent_by"] = row[6], row[7]
    return out


# -- answers ---------------------------------------------------------------------------------

_ANSWER_COLUMNS = (
    "answer_id, questionnaire_id, question_id, question, question_key, control_id, source,"
    " direction, answer, text, stages, checks, pointers, verdict_ids, library_id, model, usd,"
    " created_at, status, status_by, status_note, edited_text, status_at"
)


@dataclass(frozen=True)
class Answer:
    answer_id: str
    questionnaire_id: str
    question_id: str
    question: str
    question_key: str
    control_id: str | None
    source: str
    direction: str
    answer: str
    text: str
    stages: dict[str, Any]
    checks: list[dict[str, Any]]
    pointers: list[dict[str, Any]]
    verdict_ids: list[str]
    library_id: str | None
    model: str | None
    usd: float
    created_at: datetime
    status: Status
    status_by: str
    status_note: str
    edited_text: str | None
    status_at: datetime

    @property
    def checks_passed(self) -> bool:
        return all(c.get("passed") for c in self.checks)

    @property
    def final_text(self) -> str:
        """What a person approved: their edit, else the draft."""
        return self.edited_text if self.edited_text is not None else self.text


def _answer(r: Any) -> Answer:
    values = list(r)
    for i in (10, 11, 12, 13):
        values[i] = _load(values[i])
    return Answer(*values)


def record_answer(
    db: ScopedDB,
    *,
    questionnaire_id: str,
    question_id: str,
    question: str,
    control_id: str | None,
    source: Literal["pipeline", "library"],
    direction: str,
    answer: str,
    text: str,
    stages: dict[str, Any],
    checks: list[dict[str, Any]],
    pointers: list[dict[str, Any]],
    verdict_ids: list[str],
    library_id: str | None = None,
    model: str | None = None,
    usd: float = 0.0,
) -> str:
    answer_id = str(ULID())
    db.execute(
        "INSERT INTO questionnaire_answers (tenant_id, answer_id, questionnaire_id, question_id,"
        " question, question_key, control_id, source, direction, answer, text, stages, checks,"
        " pointers, verdict_ids, library_id, model, usd)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            answer_id,
            questionnaire_id,
            question_id,
            question,
            question_key(question),
            control_id,
            source,
            direction,
            answer,
            text,
            json.dumps(stages, default=str),
            json.dumps(checks),
            json.dumps(pointers),
            json.dumps(verdict_ids),
            library_id,
            model,
            usd,
        ),
    )
    event(db, answer_id, "draft", "ccp")
    return answer_id


def event(
    db: ScopedDB,
    answer_id: str,
    status: Status,
    actor: str,
    note: str = "",
    edited_text: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO answer_events (tenant_id, event_id, answer_id, status, actor, note,"
        " edited_text) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (db.tenant.tenant_id, str(ULID()), answer_id, status, actor, note, edited_text),
    )


def answer(db: ScopedDB, answer_id: str) -> Answer | None:
    r = db.fetch_one(
        f"SELECT {_ANSWER_COLUMNS} FROM answer_status WHERE answer_id = %s", (answer_id,)
    )
    return _answer(r) if r is not None else None


def answers_of(db: ScopedDB, questionnaire_id: str) -> list[Answer]:
    """The newest answer to each question."""
    rows = db.fetch_all(
        f"SELECT DISTINCT ON (question_id) {_ANSWER_COLUMNS} FROM answer_status"
        " WHERE questionnaire_id = %s ORDER BY question_id, created_at DESC, answer_id DESC",
        (questionnaire_id,),
    )
    return [_answer(r) for r in rows]


def approve(db: ScopedDB, answer_id: str, by: str, edited_text: str | None = None) -> Answer:
    """A person approves an answer, edited or not. A draft that failed a code check is
    approved only with the person's own text; the approved answer joins the library."""
    found = answer(db, answer_id)
    if found is None:
        raise ValueError(f"no answer {answer_id!r}")
    if found.status not in ("draft", "stale"):
        raise ValueError(f"answer {answer_id} is {found.status}")
    if not found.checks_passed and not (edited_text and edited_text.strip()):
        failed = [c["name"] for c in found.checks if not c.get("passed")]
        raise ValueError(f"the draft failed {failed}: approve it with your own text")
    edit = edited_text if edited_text and edited_text != found.text else None
    event(db, answer_id, "approved", by, "edited" if edit else "", edit)
    db.execute(
        "INSERT INTO answer_library (tenant_id, library_id, question_key, question, answer, text,"
        " pointers, verdict_ids, from_answer_id, approved_by)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            str(ULID()),
            found.question_key,
            found.question,
            found.answer,
            edit if edit is not None else found.text,
            json.dumps(found.pointers),
            json.dumps(found.verdict_ids),
            found.answer_id,
            by,
        ),
    )
    approved = answer(db, answer_id)
    assert approved is not None
    return approved


def reject(db: ScopedDB, answer_id: str, by: str, note: str) -> None:
    found = answer(db, answer_id)
    if found is None:
        raise ValueError(f"no answer {answer_id!r}")
    if not note.strip():
        raise ValueError("a rejection says why")
    event(db, answer_id, "rejected", by, note)


# -- staleness ---------------------------------------------------------------------------------


def stale_reasons(
    db: ScopedDB,
    pointers: list[dict[str, Any]],
    verdict_ids: list[str],
    heads: dict[tuple[str, str], Any] | None = None,
) -> list[str]:
    """What changed since an answer was drafted: a verdict it relied on is no longer the
    state of its subject (the status moved), or a document it cites has a newer version."""
    current = (
        heads
        if heads is not None
        else {(h.test_id, h.subject_key): h for h in bitemporal.heads(db)}
    )
    reasons: list[str] = []
    for verdict_id in verdict_ids:
        relied = verdicts.load(db, verdict_id)
        if relied is None:
            reasons.append(f"verdict_missing:{verdict_id}")
            continue
        head = current.get((relied.test_id, relied.subject_key))
        if head is not None and head.status != relied.status:
            reasons.append(
                f"verdict_changed:{relied.test_id}:{relied.status.value}->{head.status.value}"
            )
    seen: set[str] = set()
    for pointer in pointers:
        entity = str(pointer.get("evidence_entity_id") or "")
        if not entity or entity in seen:
            continue
        seen.add(entity)
        latest = evidence_version(db, entity)
        if latest is None:
            reasons.append(f"evidence_gone:{entity}")
        elif latest.source_event_id != pointer.get("source_event_id"):
            reasons.append(f"evidence_changed:{entity}")
    return reasons


@dataclass(frozen=True)
class LibraryEntry:
    library_id: str
    question_key: str
    question: str
    answer: str
    text: str
    pointers: list[dict[str, Any]]
    verdict_ids: list[str]
    from_answer_id: str
    approved_by: str
    created_at: datetime


def match(db: ScopedDB, question: str) -> tuple[LibraryEntry | None, list[str]]:
    """An approved answer to the same question that is still true: the exact question
    first, then the closest by shared words (at least 80%). A stale match is not reused;
    its reasons are returned so the pipeline can say why it drafted afresh."""
    rows = db.fetch_all(
        "SELECT DISTINCT ON (question_key) library_id, question_key, question, answer, text,"
        " pointers, verdict_ids, from_answer_id, approved_by, created_at FROM answer_library"
        " ORDER BY question_key, created_at DESC, library_id DESC"
    )
    entries = [
        LibraryEntry(r[0], r[1], r[2], r[3], r[4], _load(r[5]), _load(r[6]), r[7], r[8], r[9])
        for r in rows
    ]
    key = question_key(question)
    asked = words(question)

    def score(e: LibraryEntry) -> float:
        if e.question_key == key:
            return 2.0
        other = words(e.question)
        return len(asked & other) / len(asked | other) if asked | other else 0.0

    ranked = sorted(entries, key=score, reverse=True)
    if not ranked or score(ranked[0]) < MATCH_JACCARD:
        return None, []
    best = ranked[0]
    reasons = stale_reasons(db, best.pointers, best.verdict_ids)
    return (None, reasons) if reasons else (best, [])
