"""The execution journal (spec.md A-131): what the executor did with each grant, appended.

started   the grant is consumed (same transaction) and the call the executor will make
observed  the state it read before changing anything (the pre-state a rollback restores)
finished  the result; or
failed    the error

The operation key is the proposal id and its payload hash, so a retry names the same
operation. A started entry with no end means the executor stopped part-way; nothing retries
it blind, and a new decision is needed to try again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg
from ulid import ULID

from ccp.storage.scoped import ScopedDB

Step = Literal["started", "observed", "finished", "failed"]
Mode = Literal["simulated", "live"]


@dataclass(frozen=True)
class Entry:
    entry_id: str
    operation: str
    proposal_id: str
    grant_id: str
    entry: str
    mode: str
    detail: dict[str, Any]
    recorded_at: datetime


def operation_key(proposal_id: str, payload_sha256: str) -> str:
    return f"{proposal_id}:{payload_sha256}"


def append(
    cur: psycopg.Cursor[Any],
    tenant_id: str,
    *,
    operation: str,
    proposal_id: str,
    grant_id: str,
    entry: Step,
    mode: Mode,
    detail: dict[str, Any] | None = None,
) -> str:
    entry_id = str(ULID())
    cur.execute(
        "INSERT INTO execution_journal (entry_id, tenant_id, operation, proposal_id, grant_id,"
        " entry, mode, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            entry_id,
            tenant_id,
            operation,
            proposal_id,
            grant_id,
            entry,
            mode,
            json.dumps(detail or {}, default=str),
        ),
    )
    return entry_id


def entries(db: ScopedDB, proposal_id: str) -> list[Entry]:
    rows = db.fetch_all(
        "SELECT entry_id, operation, proposal_id, grant_id, entry, mode, detail, recorded_at"
        " FROM execution_journal WHERE proposal_id = %s ORDER BY recorded_at, entry_id",
        (proposal_id,),
    )
    return [
        Entry(
            entry_id=str(r[0]),
            operation=str(r[1]),
            proposal_id=str(r[2]),
            grant_id=str(r[3]),
            entry=str(r[4]),
            mode=str(r[5]),
            detail=dict(json.loads(r[6]) if isinstance(r[6], str) else r[6]),
            recorded_at=r[7],
        )
        for r in rows
    ]


def finished(db: ScopedDB, proposal_id: str) -> list[Entry]:
    return [e for e in entries(db, proposal_id) if e.entry == "finished"]


def in_doubt(db: ScopedDB, proposal_id: str) -> list[Entry]:
    """Started entries with no end: the executor stopped part-way through them."""
    found = entries(db, proposal_id)
    ended = {e.grant_id for e in found if e.entry in ("finished", "failed")}
    return [e for e in found if e.entry == "started" and e.grant_id not in ended]
