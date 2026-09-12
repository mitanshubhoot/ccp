"""The event log table as a queue (spec.md Layer 2). Kafka stand-in.

All functions take an open ScopedDB cursor, so row-level security scopes every statement
to the tenant the transaction was opened for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

_COLUMNS = (
    '"offset", tenant_id, topic, partition_key, source_ts, ref, produced_at, '
    "status, attempts, claimed_at, last_error"
)


@dataclass(frozen=True)
class LogRecord:
    offset: int
    tenant_id: str
    topic: str
    partition_key: str
    source_ts: datetime
    ref: str
    produced_at: datetime
    status: str
    attempts: int
    claimed_at: datetime | None
    last_error: str | None

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> LogRecord:
        return cls(*row)


def append(
    cur: psycopg.Cursor[Any],
    *,
    tenant_id: str,
    topic: str,
    partition_key: str,
    source_ts: datetime,
    ref: str,
) -> int:
    row = cur.execute(
        "INSERT INTO event_log (tenant_id, topic, partition_key, source_ts, ref)"
        ' VALUES (%s, %s, %s, %s, %s) RETURNING "offset"',
        (tenant_id, topic, partition_key, source_ts, ref),
    ).fetchone()
    assert row is not None
    return int(row[0])


def fetch_after(
    cur: psycopg.Cursor[Any], topic: str, last_offset: int, limit: int
) -> list[LogRecord]:
    rows = cur.execute(
        f'SELECT {_COLUMNS} FROM event_log WHERE topic = %s AND "offset" > %s'
        ' ORDER BY "offset" LIMIT %s',
        (topic, last_offset, limit),
    ).fetchall()
    return [LogRecord.from_row(r) for r in rows]


def read_cursor(cur: psycopg.Cursor[Any], consumer: str, topic: str) -> int:
    row = cur.execute(
        "SELECT last_offset FROM consumer_offsets WHERE consumer = %s AND topic = %s",
        (consumer, topic),
    ).fetchone()
    return int(row[0]) if row else 0


def advance_cursor(cur: psycopg.Cursor[Any], consumer: str, topic: str, last_offset: int) -> None:
    cur.execute(
        "INSERT INTO consumer_offsets (consumer, topic, last_offset) VALUES (%s, %s, %s)"
        " ON CONFLICT (consumer, topic) DO UPDATE SET last_offset = EXCLUDED.last_offset",
        (consumer, topic, last_offset),
    )


def claim(
    cur: psycopg.Cursor[Any], topic: str, limit: int, visibility_seconds: int
) -> list[LogRecord]:
    """Claim pending rows, or claimed rows whose visibility timeout has expired."""
    rows = cur.execute(
        "UPDATE event_log SET status = 'claimed', claimed_at = now(), attempts = attempts + 1"
        ' WHERE "offset" IN ('
        '   SELECT "offset" FROM event_log'
        "   WHERE topic = %s AND ("
        "     status = 'pending'"
        "     OR (status = 'claimed' AND claimed_at < now() - make_interval(secs => %s))"
        "   )"
        '   ORDER BY "offset" LIMIT %s FOR UPDATE SKIP LOCKED'
        f") RETURNING {_COLUMNS}",
        (topic, visibility_seconds, limit),
    ).fetchall()
    return [LogRecord.from_row(r) for r in rows]


def mark_done(cur: psycopg.Cursor[Any], offset: int) -> None:
    cur.execute(
        "UPDATE event_log SET status = 'done', last_error = NULL WHERE \"offset\" = %s", (offset,)
    )


def mark_retry(cur: psycopg.Cursor[Any], offset: int, error: str) -> None:
    cur.execute(
        "UPDATE event_log SET status = 'pending', claimed_at = NULL, last_error = %s"
        ' WHERE "offset" = %s',
        (error[:2000], offset),
    )


def mark_parked(cur: psycopg.Cursor[Any], offset: int, reason: str) -> None:
    cur.execute(
        "UPDATE event_log SET status = 'parked', claimed_at = NULL, last_error = %s"
        ' WHERE "offset" = %s',
        (reason[:2000], offset),
    )


def mark_dead(cur: psycopg.Cursor[Any], record: LogRecord, error: str) -> None:
    cur.execute(
        "UPDATE event_log SET status = 'dead', last_error = %s WHERE \"offset\" = %s",
        (error[:2000], record.offset),
    )
    cur.execute(
        'INSERT INTO dead_letter ("offset", tenant_id, topic, attempts, last_error)'
        " VALUES (%s, %s, %s, %s, %s)",
        (record.offset, record.tenant_id, record.topic, record.attempts, error[:2000]),
    )


def status_of(cur: psycopg.Cursor[Any], offset: int) -> tuple[str, int, str | None]:
    row = cur.execute(
        'SELECT status, attempts, last_error FROM event_log WHERE "offset" = %s', (offset,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no event_log row at offset {offset}")
    return str(row[0]), int(row[1]), row[2]
