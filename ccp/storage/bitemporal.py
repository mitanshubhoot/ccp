"""The append-only, bitemporal compliance-state table (spec.md Layer 5).

Two time axes. Valid time: when the fact was true in the world (valid_from, valid_to).
Transaction time: when we recorded it (recorded_seq, recorded_at). A correction is a new
row referencing the row it supersedes. There is no UPDATE statement in this module, and
the database grants none.

Supersession timeline for the worked example (one subject):

   valid time  ─────────────────────────────────────────────────►
   r1  pass    [Mar 1 ─────────────────────────────────────►   recorded Mar 1
   r2  fail              [Mar 4 ────────────────────────►      recorded Mar 10, supersedes r1
   r3  fail    [Mar 1 ─────────────────────────────────────►   recorded Mar 12, supersedes r2

Rules the queries apply:
  * a row is known at K if recorded_at <= K;
  * a row's validity ends at its own valid_to, else at the valid_from of the known row
    that supersedes it, else never;
  * among known rows covering the instant asked for, the latest recorded belief wins.

Writer guarantees:
  * per-subject advisory lock: two writers on one subject serialize into one chain;
  * per-tenant sequence, incremented inside the same transaction, so recorded order
    equals commit order and believed_state(known_at) is stable across runs.

A late fact (A-107): a verdict about an earlier moment than the head's, recorded after it,
can be filed into history instead of superseding the head. The row is bounded at the next
newer fact's valid_from, supersedes nothing and is never a head, so the as-of queries see it
for the past it describes and the newer belief stays current. Before, whichever verdict
arrived last became the head, so a stale evaluation that finished late replaced a newer
one. Explicit corrections still supersede, and may rewrite the past.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from ulid import ULID

from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus

Reason = Literal["evaluation", "correction", "human_override", "late_event", "stale_evidence"]

_COLUMNS = (
    "row_id, tenant_id, subject_key, test_id, control_ids, status, verdict_id, valid_from,"
    " valid_to, recorded_seq, recorded_at, supersedes_row_id, reason, history"
)


@dataclass(frozen=True)
class StateRow:
    row_id: str
    tenant_id: str
    subject_key: str
    test_id: str
    control_ids: tuple[str, ...]
    status: VerdictStatus
    verdict_id: str
    valid_from: datetime
    valid_to: datetime | None
    recorded_seq: int
    recorded_at: datetime
    supersedes_row_id: str | None
    reason: str
    history: bool = False  # a late fact filed into history: never a head (A-107)

    @classmethod
    def from_row(cls, r: tuple[Any, ...]) -> StateRow:
        control_ids = json.loads(r[4]) if isinstance(r[4], str) else r[4]
        return cls(
            row_id=r[0],
            tenant_id=r[1],
            subject_key=r[2],
            test_id=r[3],
            control_ids=tuple(control_ids),
            status=VerdictStatus(r[5]),
            verdict_id=r[6],
            valid_from=r[7],
            valid_to=r[8],
            recorded_seq=int(r[9]),
            recorded_at=r[10],
            supersedes_row_id=r[11],
            reason=r[12],
            history=bool(r[13]),
        )


def _next_seq(cur: Any, tenant_id: str, name: str = "compliance_state") -> int:
    """Advance the tenant's counter under its row lock; the lock is held until commit."""
    row = cur.execute(
        "INSERT INTO tenant_sequences (tenant_id, name, value) VALUES (%s, %s, 1)"
        " ON CONFLICT (tenant_id, name) DO UPDATE SET value = tenant_sequences.value + 1"
        " RETURNING value",
        (tenant_id, name),
    ).fetchone()
    assert row is not None
    return int(row[0])


_HEAD = (
    "NOT c.history"
    " AND NOT EXISTS (SELECT 1 FROM compliance_state s WHERE s.supersedes_row_id = c.row_id)"
)


def _open_row(cur: Any, subject_key: str) -> StateRow | None:
    """The head of the subject's chain: the row nobody supersedes, never a history row."""
    row = cur.execute(
        f"SELECT {_COLUMNS} FROM compliance_state c WHERE c.subject_key = %s AND {_HEAD}"
        " ORDER BY c.recorded_seq DESC LIMIT 1",
        (subject_key,),
    ).fetchone()
    return StateRow.from_row(row) if row else None


def _next_fact(cur: Any, subject_key: str, after: datetime) -> datetime | None:
    """The valid_from of the subject's next newer fact: where a late fact's validity ends."""
    row = cur.execute(
        "SELECT min(valid_from) FROM compliance_state WHERE subject_key = %s AND valid_from > %s",
        (subject_key, after),
    ).fetchone()
    return row[0] if row else None


def record(
    db: ScopedDB,
    *,
    subject_key: str,
    test_id: str,
    control_ids: tuple[str, ...] | list[str],
    status: VerdictStatus | str,
    verdict_id: str,
    valid_from: datetime,
    valid_to: datetime | None = None,
    reason: Reason = "evaluation",
    late_into_history: bool = False,
) -> StateRow:
    """Append one row for the subject, superseding the chain head if there is one.

    With `late_into_history`, a fact older than the head (valid_from before the head's) is
    filed into history instead (A-107): bounded at the next newer fact, superseding nothing,
    labelled `late_event` unless it carries a reason of its own. The choice is made under the
    subject's lock, against the head as it stands. Lock, read the head, stamp the sequence,
    insert, commit: one short transaction.
    """
    status_value = VerdictStatus(status).value
    with db.transaction() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"{db.tenant.tenant_id}|{subject_key}",),
        )
        head = _open_row(cur, subject_key)
        history = late_into_history and head is not None and valid_from < head.valid_from
        supersedes = None if history or head is None else head.row_id
        if history:
            valid_to = _next_fact(cur, subject_key, valid_from)
            reason = "late_event" if reason == "evaluation" else reason
        seq = _next_seq(cur, db.tenant.tenant_id)
        row_id = str(ULID())
        recorded_at = datetime.now(UTC)
        inserted = cur.execute(
            "INSERT INTO compliance_state (row_id, tenant_id, subject_key, test_id, control_ids,"
            " status, verdict_id, valid_from, valid_to, recorded_seq, recorded_at,"
            " supersedes_row_id, reason, history)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {_COLUMNS}",
            (
                row_id,
                db.tenant.tenant_id,
                subject_key,
                test_id,
                json.dumps(list(control_ids)),
                status_value,
                verdict_id,
                valid_from,
                valid_to,
                seq,
                recorded_at,
                supersedes,
                reason,
                history,
            ),
        ).fetchone()
        assert inserted is not None
        return StateRow.from_row(inserted)


_AS_OF = f"""
WITH known AS (
  SELECT c.*, COALESCE(c.valid_to, s.valid_from) AS effective_valid_to
  FROM compliance_state c
  LEFT JOIN compliance_state s
    ON s.supersedes_row_id = c.row_id AND s.recorded_at <= %(known_at)s
  WHERE c.subject_key = %(subject)s AND c.recorded_at <= %(known_at)s
)
SELECT {_COLUMNS} FROM known
WHERE valid_from <= %(valid_at)s
  AND (effective_valid_to IS NULL OR %(valid_at)s < effective_valid_to)
ORDER BY recorded_seq DESC
LIMIT 1
"""


def state_as_of(
    db: ScopedDB, subject_key: str, valid_at: datetime, known_at: datetime
) -> StateRow | None:
    """What was true at valid_at, according to everything recorded by known_at."""
    row = db.fetch_one(_AS_OF, {"subject": subject_key, "valid_at": valid_at, "known_at": known_at})
    return StateRow.from_row(row) if row else None


def current_state(
    db: ScopedDB, subject_key: str, valid_at: datetime | None = None
) -> StateRow | None:
    """Our latest belief about valid_at (default: now)."""
    now = datetime.now(UTC)
    return state_as_of(db, subject_key, valid_at or now, now)


def believed_state(db: ScopedDB, subject_key: str, at: datetime) -> StateRow | None:
    """What we believed about `at`, as of `at` itself."""
    return state_as_of(db, subject_key, at, at)


def chain(db: ScopedDB, subject_key: str) -> list[StateRow]:
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM compliance_state WHERE subject_key = %s ORDER BY recorded_seq",
        (subject_key,),
    )
    return [StateRow.from_row(r) for r in rows]


def heads(db: ScopedDB) -> list[StateRow]:
    """Every subject's current chain head; the report reads these. History rows are never
    heads (A-107)."""
    rows = db.fetch_all(
        f"SELECT {_COLUMNS} FROM compliance_state c WHERE {_HEAD} ORDER BY subject_key"
    )
    return [StateRow.from_row(r) for r in rows]
