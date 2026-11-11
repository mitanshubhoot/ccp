"""Fix suggestions as records (spec.md A-159): what was suggested, and what became of it.

A suggestion is never edited. Its row holds the rendered fix and the hash that covers it;
each change of status is an event. One live suggestion per (test, resource, fix): the same
rendering again is the same suggestion, and a different one (the code moved, a new template
version) supersedes it.

    open        offered to a person
    accepted    a person says they will apply it (CCP still waits for a read that sees it)
    dismissed   a person says it does not apply, with a note
    confirmed   a later verdict's read saw the resource pass: the only way a fix is done
    reopened    the resource failed again within 30 days of a confirmation
    superseded  a newer rendering replaced it
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ulid import ULID

from ccp.remediation.fixes import Rendered
from ccp.storage.scoped import ScopedDB

Status = Literal["open", "accepted", "dismissed", "confirmed", "reopened", "superseded"]
LIVE: tuple[Status, ...] = ("open", "accepted", "reopened")  # still waiting for a fix
PEOPLE: tuple[Status, ...] = ("accepted", "dismissed")  # what a person may record

_COLUMNS = (
    "suggestion_id, verdict_id, test_id, resource, source, investigation_id, fix, fix_version,"
    " template_sha256, params, body, checks, lookup_ids, payload_sha256, created_at, status,"
    " status_by, status_note, status_verdict_id, status_at"
)


@dataclass(frozen=True)
class Suggestion:
    suggestion_id: str
    verdict_id: str
    test_id: str
    resource: str
    source: str
    investigation_id: str | None
    fix: str
    fix_version: int
    template_sha256: str
    params: dict[str, str]
    body: dict[str, Any]
    checks: list[dict[str, Any]]
    lookup_ids: list[str]
    payload_sha256: str
    created_at: datetime
    status: Status
    status_by: str
    status_note: str
    status_verdict_id: str | None
    status_at: datetime


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _row(r: Any) -> Suggestion:
    return Suggestion(
        r[0],
        r[1],
        r[2],
        r[3],
        r[4],
        r[5],
        r[6],
        r[7],
        r[8],
        _load(r[9]),
        _load(r[10]),
        _load(r[11]),
        _load(r[12]),
        r[13],
        r[14],
        r[15],
        r[16],
        r[17],
        r[18],
        r[19],
    )


def get(db: ScopedDB, suggestion_id: str) -> Suggestion | None:
    r = db.fetch_one(
        f"SELECT {_COLUMNS} FROM fix_suggestion_status WHERE suggestion_id = %s",
        (suggestion_id,),
    )
    return _row(r) if r is not None else None


def list_all(
    db: ScopedDB, status: str | None = None, test_id: str | None = None, limit: int = 100
) -> list[Suggestion]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM fix_suggestion_status"
        " WHERE (%s::text IS NULL OR status = %s) AND (%s::text IS NULL OR test_id = %s)"
        " ORDER BY created_at DESC, suggestion_id DESC LIMIT %s",
        (status, status, test_id, test_id, limit),
    )
    return [_row(r) for r in rows]


def for_verdict(db: ScopedDB, verdict_id: str) -> list[Suggestion]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM fix_suggestion_status WHERE verdict_id = %s"
        " ORDER BY created_at, suggestion_id",
        (verdict_id,),
    )
    return [_row(r) for r in rows]


def live_for(db: ScopedDB, test_id: str, resource: str) -> list[Suggestion]:
    """The suggestions for a resource still waiting for a read that sees the fix."""
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM fix_suggestion_status"
        " WHERE test_id = %s AND resource = %s AND status = ANY(%s)"
        " ORDER BY created_at, suggestion_id",
        (test_id, resource, list(LIVE)),
    )
    return [_row(r) for r in rows]


def record_event(
    db: ScopedDB,
    suggestion_id: str,
    status: Status,
    actor: str,
    note: str = "",
    verdict_id: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO fix_suggestion_events (tenant_id, event_id, suggestion_id, status, actor,"
        " note, verdict_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (db.tenant.tenant_id, str(ULID()), suggestion_id, status, actor, note, verdict_id),
    )


def suggest(
    db: ScopedDB,
    *,
    verdict_id: str,
    test_id: str,
    resource: str,
    source: Literal["code", "model"],
    rendered: Rendered,
    investigation_id: str | None = None,
    lookup_ids: list[str] | None = None,
) -> tuple[str, bool]:
    """Offer a rendered fix: (the suggestion id, whether it is new). A rendering that failed
    an offline check is refused here too, whoever asks."""
    if not rendered.passed:
        raise ValueError(f"{rendered.fix.fix_id} failed {[c.name for c in rendered.failed]}")
    payload = rendered.payload_sha256()
    for live in live_for(db, test_id, resource):
        if live.fix != rendered.fix.fix_id:
            continue
        if live.payload_sha256 == payload:
            return live.suggestion_id, False
        record_event(db, live.suggestion_id, "superseded", "ccp", "a newer rendering replaced it")
    suggestion_id = str(ULID())
    db.execute(
        "INSERT INTO fix_suggestions (tenant_id, suggestion_id, verdict_id, test_id, resource,"
        " source, investigation_id, fix, fix_version, template_sha256, params, body, checks,"
        " lookup_ids, payload_sha256)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            suggestion_id,
            verdict_id,
            test_id,
            resource,
            source,
            investigation_id,
            rendered.fix.fix_id,
            rendered.fix.version,
            rendered.fix.sha256,
            json.dumps(rendered.params),
            json.dumps(rendered.body()),
            json.dumps([c.__dict__ for c in rendered.checks]),
            json.dumps(lookup_ids or []),
            payload,
        ),
    )
    record_event(db, suggestion_id, "open", source)
    return suggestion_id, True
