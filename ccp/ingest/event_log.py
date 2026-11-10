"""The event log table as a queue (spec.md Layer 2). Kafka stand-in.

All functions take an open ScopedDB cursor, so row-level security scopes every statement
to the tenant the transaction was opened for.

Retries wait (A-97): a failed parallel record is pending again only after
``next_attempt_at``, an exponential backoff with jitter; a throttle (429, 529, 503) defers the
record without spending an attempt. Ordered topics keep their failure state per consumer in
``ordered_failures``, because several consumers read the same ordered topic and one
consumer's dead letter must not hide the record from the others.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from ulid import ULID

_COLUMNS = (
    '"offset", tenant_id, topic, partition_key, source_ts, ref, produced_at, '
    "status, attempts, claimed_at, last_error"
)
BACKOFF_CAP_SECONDS = 900.0


def backoff_seconds(n: int, base: float) -> float:
    """The wait before try n+1: base * 2^(n-1), capped, stretched by up to half again at
    random so a burst of failures does not retry in lockstep. Zero when base is zero."""
    if base <= 0:
        return 0.0
    delay = min(BACKOFF_CAP_SECONDS, base * 2.0 ** max(n - 1, 0))
    return float(delay * random.uniform(1.0, 1.5))  # noqa: S311 - jitter, not security


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


def get(cur: psycopg.Cursor[Any], offset: int) -> LogRecord | None:
    row = cur.execute(f'SELECT {_COLUMNS} FROM event_log WHERE "offset" = %s', (offset,)).fetchone()
    return LogRecord.from_row(row) if row else None


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
    """Claim pending rows whose wait is over, or claimed rows whose visibility timeout has
    expired."""
    rows = cur.execute(
        "UPDATE event_log SET status = 'claimed', claimed_at = now(), attempts = attempts + 1"
        ' WHERE "offset" IN ('
        '   SELECT "offset" FROM event_log'
        "   WHERE topic = %s AND ("
        "     (status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= now()))"
        "     OR (status = 'claimed' AND claimed_at < now() - make_interval(secs => %s))"
        "   )"
        '   ORDER BY "offset" LIMIT %s FOR UPDATE SKIP LOCKED'
        f") RETURNING {_COLUMNS}",
        (topic, visibility_seconds, limit),
    ).fetchall()
    return [LogRecord.from_row(r) for r in rows]


def mark_done(cur: psycopg.Cursor[Any], offset: int) -> None:
    cur.execute(
        "UPDATE event_log SET status = 'done', last_error = NULL, next_attempt_at = NULL"
        ' WHERE "offset" = %s',
        (offset,),
    )


def mark_retry(
    cur: psycopg.Cursor[Any], offset: int, error: str, delay_seconds: float = 0.0
) -> None:
    cur.execute(
        "UPDATE event_log SET status = 'pending', claimed_at = NULL, last_error = %s,"
        ' next_attempt_at = now() + make_interval(secs => %s) WHERE "offset" = %s',
        (error[:2000], delay_seconds, offset),
    )


def mark_deferred(cur: psycopg.Cursor[Any], offset: int, reason: str, delay_seconds: float) -> None:
    """A throttle: pending again after the wait, and the claim's attempt is given back."""
    cur.execute(
        "UPDATE event_log SET status = 'pending', claimed_at = NULL, last_error = %s,"
        " attempts = greatest(attempts - 1, 0), deferrals = deferrals + 1,"
        ' next_attempt_at = now() + make_interval(secs => %s) WHERE "offset" = %s',
        (reason[:2000], delay_seconds, offset),
    )


def deferrals_of(cur: psycopg.Cursor[Any], offset: int) -> int:
    row = cur.execute('SELECT deferrals FROM event_log WHERE "offset" = %s', (offset,)).fetchone()
    return int(row[0]) if row else 0


def mark_parked(cur: psycopg.Cursor[Any], offset: int, reason: str) -> None:
    cur.execute(
        "UPDATE event_log SET status = 'parked', claimed_at = NULL, last_error = %s"
        ' WHERE "offset" = %s',
        (reason[:2000], offset),
    )


def mark_dead(cur: psycopg.Cursor[Any], record: LogRecord, error: str) -> None:
    cur.execute(
        "UPDATE event_log SET status = 'dead', last_error = %s, next_attempt_at = NULL"
        ' WHERE "offset" = %s',
        (error[:2000], record.offset),
    )
    cur.execute(  # a replayed record that dies again keeps its first dead-letter row
        'INSERT INTO dead_letter ("offset", tenant_id, topic, attempts, last_error)'
        ' VALUES (%s, %s, %s, %s, %s) ON CONFLICT ("offset") DO NOTHING',
        (record.offset, record.tenant_id, record.topic, record.attempts, error[:2000]),
    )


def requeue_dead(cur: psycopg.Cursor[Any], offset: int) -> bool:
    """A dead parallel record goes back to pending with fresh attempts."""
    row = cur.execute(
        "UPDATE event_log SET status = 'pending', attempts = 0, deferrals = 0, claimed_at = NULL,"
        ' next_attempt_at = NULL WHERE "offset" = %s AND status = \'dead\' RETURNING "offset"',
        (offset,),
    ).fetchone()
    return row is not None


def status_of(cur: psycopg.Cursor[Any], offset: int) -> tuple[str, int, str | None]:
    row = cur.execute(
        'SELECT status, attempts, last_error FROM event_log WHERE "offset" = %s', (offset,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no event_log row at offset {offset}")
    return str(row[0]), int(row[1]), row[2]


def db_now(cur: psycopg.Cursor[Any]) -> datetime:
    row = cur.execute("SELECT now()").fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


# -- ordered topics: failure state per consumer -------------------------------------------


@dataclass(frozen=True)
class OrderedFailure:
    consumer: str
    offset: int
    tenant_id: str
    topic: str
    attempts: int
    status: str  # retrying | dead | resolved
    last_error: str | None
    next_attempt_at: datetime | None
    updated_at: datetime


_FAILURE_COLUMNS = (
    'consumer, "offset", tenant_id, topic, attempts, status, last_error, next_attempt_at,'
    " updated_at"
)


def ordered_failures(
    cur: psycopg.Cursor[Any], consumer: str, offsets: list[int]
) -> dict[int, OrderedFailure]:
    if not offsets:
        return {}
    rows = cur.execute(
        f"SELECT {_FAILURE_COLUMNS} FROM ordered_failures"
        ' WHERE consumer = %s AND "offset" = ANY(%s)',
        (consumer, offsets),
    ).fetchall()
    return {int(r[1]): OrderedFailure(*r) for r in rows}


def record_ordered_failure(
    cur: psycopg.Cursor[Any],
    consumer: str,
    record: LogRecord,
    error: str,
    *,
    terminal: bool,
    max_attempts: int,
    base_delay: float,
) -> OrderedFailure:
    """Count one failed try; past max_attempts (or at once, when terminal) it is dead."""
    row = cur.execute(
        'INSERT INTO ordered_failures (consumer, "offset", tenant_id, topic, attempts, last_error)'
        " VALUES (%s, %s, %s, %s, 1, %s)"
        ' ON CONFLICT (consumer, "offset") DO UPDATE SET'
        " attempts = ordered_failures.attempts + 1, last_error = EXCLUDED.last_error,"
        " status = 'retrying', updated_at = now()"
        " RETURNING attempts",
        (consumer, record.offset, record.tenant_id, record.topic, error[:2000]),
    ).fetchone()
    assert row is not None
    attempts = int(row[0])
    dead = terminal or attempts >= max_attempts
    cur.execute(
        "UPDATE ordered_failures SET status = %s,"
        " next_attempt_at = CASE WHEN %s THEN NULL ELSE now() + make_interval(secs => %s) END"
        ' WHERE consumer = %s AND "offset" = %s',
        (
            "dead" if dead else "retrying",
            dead,
            backoff_seconds(attempts, base_delay),
            consumer,
            record.offset,
        ),
    )
    found = ordered_failures(cur, consumer, [record.offset])
    return found[record.offset]


def resolve_ordered_failure(cur: psycopg.Cursor[Any], consumer: str, offset: int) -> None:
    cur.execute(
        "UPDATE ordered_failures SET status = 'resolved', next_attempt_at = NULL,"
        ' updated_at = now() WHERE consumer = %s AND "offset" = %s',
        (consumer, offset),
    )


def dead_ordered(cur: psycopg.Cursor[Any], consumer: str | None = None) -> list[OrderedFailure]:
    clause = " AND consumer = %s" if consumer else ""
    params: tuple[Any, ...] = (consumer,) if consumer else ()
    rows = cur.execute(
        f"SELECT {_FAILURE_COLUMNS} FROM ordered_failures WHERE status = 'dead'{clause}"
        ' ORDER BY "offset"',
        params,
    ).fetchall()
    return [OrderedFailure(*r) for r in rows]


def dead_parallel(cur: psycopg.Cursor[Any], topic: str | None = None) -> list[LogRecord]:
    clause = " AND topic = %s" if topic else ""
    params: tuple[Any, ...] = (topic,) if topic else ()
    rows = cur.execute(
        f"SELECT {_COLUMNS} FROM event_log WHERE status = 'dead'{clause} ORDER BY \"offset\"",
        params,
    ).fetchall()
    return [LogRecord.from_row(r) for r in rows]


def record_replay(
    cur: psycopg.Cursor[Any],
    *,
    tenant_id: str,
    topic: str,
    offset: int,
    outcome: str,
    replayed_by: str,
    consumer: str | None = None,
    detail: str | None = None,
) -> None:
    cur.execute(
        'INSERT INTO dead_letter_replays (replay_id, tenant_id, topic, consumer, "offset",'
        " outcome, detail, replayed_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            str(ULID()),
            tenant_id,
            topic,
            consumer,
            offset,
            outcome,
            (detail or "")[:2000] or None,
            replayed_by,
        ),
    )
