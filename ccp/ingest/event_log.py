"""The event log table as a queue (spec.md Layer 2). Kafka stand-in.

All functions take an open ScopedDB cursor, so row-level security scopes every statement
to the tenant the transaction was opened for.

Retries wait (A-97): a failed parallel record is pending again only after
``next_attempt_at``, an exponential backoff with jitter; a throttle (429, 529, 503) defers the
record without spending an attempt. Ordered topics keep their failure state per consumer in
``ordered_failures``, because several consumers read the same ordered topic and one
consumer's dead letter must not hide the record from the others.

Ordered topics are read by seq, not by offset (A-104). The global offset is numbered when a
row is inserted but becomes visible when it commits, so a reader that jumped to the highest
offset it saw could pass a lower one that committed later and never read it. A record on an
ordered topic takes the next number of its (tenant, topic) counter, whose row the producing
transaction holds until it commits: producers on one tenant's topic take turns, numbers
become visible in order, and a cursor never passes a number that has yet to appear.

A parallel claim is a lease (A-105): a fresh token and an expiry that the holder's heartbeat
pushes forward while it works. Another worker re-claims the record only once the lease has
run out, and every status change after the claim presents the token, so a worker that lost
its lease (a long pause, a dropped connection) changes nothing.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from ulid import ULID

from ccp.ingest.topics import TopicKind, kind_of

_COLUMNS = (
    '"offset", tenant_id, topic, partition_key, source_ts, ref, produced_at, '
    "status, attempts, claimed_at, last_error, seq, lease_token"
)
# Leaving the claimed state clears the lease, so no token matches a record nobody holds.
_RELEASE = "lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL"
_QUALIFIED = ", ".join(f"e.{c.strip()}" for c in _COLUMNS.split(","))
BACKOFF_CAP_SECONDS = 900.0
# Producers notify workers on this channel when a record appears (A-106): '<tenant>:<topic>'.
CHANNEL = "ccp_work"


def notify(cur: psycopg.Cursor[Any], tenant_id: str, topic: str) -> None:
    """Wake the workers of `topic` when this transaction commits. Postgres sends nothing
    for a rolled-back transaction and folds repeats within one transaction into one."""
    cur.execute("SELECT pg_notify(%s, %s)", (CHANNEL, f"{tenant_id}:{topic}"))


def try_lock(cur: psycopg.Cursor[Any], key: str) -> bool:
    """A lock on `key` held until this transaction ends; False when another holds it."""
    row = cur.execute(
        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", (key,)
    ).fetchone()
    return bool(row and row[0])


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
    seq: int | None = None  # ordered topics: its place in the tenant's topic (A-104)
    lease_token: str | None = None  # parallel topics: the claim this copy was handed (A-105)

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> LogRecord:
        return cls(*row)

    @property
    def position(self) -> int:
        """The record's place in its ordered topic."""
        if self.seq is None:
            raise ValueError(f"offset {self.offset} on {self.topic!r} has no seq")
        return self.seq


def _next_seq(cur: psycopg.Cursor[Any], tenant_id: str, topic: str) -> int:
    """Advance the (tenant, topic) counter; its row stays locked until this transaction
    ends, so the next producer on the same topic waits for the commit (A-104)."""
    row = cur.execute(
        "INSERT INTO tenant_sequences (tenant_id, name, value) VALUES (%s, %s, 1)"
        " ON CONFLICT (tenant_id, name) DO UPDATE SET value = tenant_sequences.value + 1"
        " RETURNING value",
        (tenant_id, f"topic:{topic}"),
    ).fetchone()
    assert row is not None
    return int(row[0])


def append(
    cur: psycopg.Cursor[Any],
    *,
    tenant_id: str,
    topic: str,
    partition_key: str,
    source_ts: datetime,
    ref: str,
) -> int:
    """Append one record and return its offset. A record on an ordered topic also takes
    the next seq of its tenant's topic. The topic's workers are notified at commit."""
    seq = _next_seq(cur, tenant_id, topic) if kind_of(topic) is TopicKind.ordered else None
    row = cur.execute(
        "INSERT INTO event_log (tenant_id, topic, partition_key, source_ts, ref, seq)"
        ' VALUES (%s, %s, %s, %s, %s, %s) RETURNING "offset"',
        (tenant_id, topic, partition_key, source_ts, ref, seq),
    ).fetchone()
    assert row is not None
    notify(cur, tenant_id, topic)
    return int(row[0])


def get(cur: psycopg.Cursor[Any], offset: int) -> LogRecord | None:
    row = cur.execute(f'SELECT {_COLUMNS} FROM event_log WHERE "offset" = %s', (offset,)).fetchone()
    return LogRecord.from_row(row) if row else None


def fetch_after(cur: psycopg.Cursor[Any], topic: str, last_seq: int, limit: int) -> list[LogRecord]:
    """The tenant's next records on an ordered topic, in seq order (row-level security
    scopes the read to the transaction's tenant)."""
    rows = cur.execute(
        f"SELECT {_COLUMNS} FROM event_log WHERE topic = %s AND seq > %s ORDER BY seq LIMIT %s",
        (topic, last_seq, limit),
    ).fetchall()
    return [LogRecord.from_row(r) for r in rows]


def committed_seq(cur: psycopg.Cursor[Any], topic: str) -> int:
    """The highest seq the tenant's topic has committed (0 when empty)."""
    row = cur.execute(
        "SELECT coalesce(max(seq), 0) FROM event_log WHERE topic = %s", (topic,)
    ).fetchone()
    return int(row[0]) if row else 0


def read_cursor(cur: psycopg.Cursor[Any], consumer: str, topic: str) -> int:
    """The last seq the consumer completed on the topic."""
    row = cur.execute(
        "SELECT last_seq FROM consumer_offsets WHERE consumer = %s AND topic = %s",
        (consumer, topic),
    ).fetchone()
    return int(row[0]) if row else 0


def advance_cursor(cur: psycopg.Cursor[Any], consumer: str, topic: str, last_seq: int) -> None:
    cur.execute(
        "INSERT INTO consumer_offsets (consumer, topic, last_seq) VALUES (%s, %s, %s)"
        " ON CONFLICT (consumer, topic) DO UPDATE SET last_seq = EXCLUDED.last_seq",
        (consumer, topic, last_seq),
    )


# A record a claim may take: pending with its wait over, or claimed under a lease that ran out.
_DUE = (
    "((e.status = 'pending' AND (e.next_attempt_at IS NULL OR e.next_attempt_at <= now()))"
    " OR (e.status = 'claimed' AND coalesce(e.lease_expires_at,"
    " e.claimed_at + make_interval(secs => %(lease)s)) < now()))"
)
# Another record of the same partition key held under a live lease (A-107).
_KEY_HELD = (
    "EXISTS (SELECT 1 FROM event_log c WHERE c.topic = e.topic"
    ' AND c.partition_key = e.partition_key AND c."offset" <> e."offset"'
    " AND c.status = 'claimed' AND coalesce(c.lease_expires_at,"
    " c.claimed_at + make_interval(secs => %(lease)s)) > now())"
)
_TAKE = (
    "UPDATE event_log e SET status = 'claimed', claimed_at = now(),"
    " attempts = e.attempts + 1, lease_token = gen_random_uuid()::text,"
    " lease_owner = %(owner)s, lease_expires_at = now() + make_interval(secs => %(lease)s)"
)


def claim(
    cur: psycopg.Cursor[Any],
    topic: str,
    limit: int,
    lease_seconds: float,
    owner: str = "",
    *,
    one_per_key: bool = False,
) -> list[LogRecord]:
    """Claim up to `limit` records: pending ones whose wait is over, and claimed ones whose
    lease has run out. Each claim is a lease of `lease_seconds` with a fresh token, which the
    holder's heartbeat renews and every later status change must present (A-105). A claim
    from before leases has no expiry and runs out `lease_seconds` after it was made.

    At most `limit`: the pick is a materialized CTE, run once. As `IN (SELECT ... LIMIT n FOR
    UPDATE SKIP LOCKED)` the planner could rescan the subquery once per row of a nested loop,
    and every rescan skipped the rows this statement had just claimed, so one pass took the
    whole topic past the tenant's concurrency cap (A-104).

    `one_per_key` (A-107): never a record whose partition key has another record under a live
    lease, and at most one per key, the oldest first. Two claimers racing for one key are
    kept apart by a transaction lock on the key: the second skips it, and once the first
    commits, its live claim keeps the key closed."""
    params = {"topic": topic, "lease": lease_seconds, "limit": limit, "owner": owner or None}
    if not one_per_key:
        rows = cur.execute(
            "WITH picked AS MATERIALIZED ("
            f'   SELECT "offset" FROM event_log e WHERE e.topic = %(topic)s AND {_DUE}'
            '   ORDER BY "offset" LIMIT %(limit)s FOR UPDATE SKIP LOCKED'
            f") {_TAKE}"
            ' FROM picked WHERE e."offset" = picked."offset"'
            f" RETURNING {_QUALIFIED}",
            params,
        ).fetchall()
        return sorted((LogRecord.from_row(r) for r in rows), key=lambda r: r.offset)
    candidates = cur.execute(
        f'SELECT e."offset", e.partition_key FROM event_log e WHERE e.topic = %(topic)s'
        f" AND {_DUE} AND NOT {_KEY_HELD}"
        ' ORDER BY e."offset" LIMIT %(scan)s FOR UPDATE SKIP LOCKED',
        {**params, "scan": limit * 4 + 8},
    ).fetchall()
    chosen: list[int] = []
    keys: set[str] = set()
    for offset, key in candidates:
        if len(chosen) >= limit:
            break
        if key in keys or not try_lock(cur, f"subject:{topic}:{key}"):
            continue  # one per key here; another claimer is taking this key right now
        held = cur.execute(  # a claim on this key committed since the candidates were read
            f'SELECT {_KEY_HELD} FROM event_log e WHERE e."offset" = %(offset)s',
            {**params, "offset": offset},
        ).fetchone()
        if held is not None and held[0]:
            continue
        keys.add(str(key))
        chosen.append(int(offset))
    if not chosen:
        return []
    rows = cur.execute(
        f'{_TAKE} WHERE e."offset" = ANY(%(chosen)s) RETURNING {_QUALIFIED}',
        {**params, "chosen": chosen},
    ).fetchall()
    return sorted((LogRecord.from_row(r) for r in rows), key=lambda r: r.offset)


def live_claims(cur: psycopg.Cursor[Any], topic: str, lease_seconds: float) -> int:
    """Records of the tenant's topic held under a live lease (A-105, A-107)."""
    row = cur.execute(
        "SELECT count(*) FROM event_log WHERE topic = %s AND status = 'claimed'"
        " AND coalesce(lease_expires_at, claimed_at + make_interval(secs => %s)) > now()",
        (topic, lease_seconds),
    ).fetchone()
    return int(row[0]) if row else 0


def lock(cur: psycopg.Cursor[Any], key: str) -> None:
    """A lock on `key` held until this transaction ends, waiting for it if need be."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


def waiting_on_key(cur: psycopg.Cursor[Any], record: LogRecord) -> bool:
    """Whether another record of the same partition key is pending (A-107)."""
    row = cur.execute(
        "SELECT 1 FROM event_log WHERE topic = %s AND partition_key = %s"
        " AND status = 'pending' AND \"offset\" <> %s LIMIT 1",
        (record.topic, record.partition_key, record.offset),
    ).fetchone()
    return row is not None


def renew(cur: psycopg.Cursor[Any], held: Mapping[int, str], lease_seconds: float) -> set[int]:
    """Push the given leases (offset -> token) forward. Returns the offsets still held; a
    missing one was re-claimed by another worker after its lease ran out."""
    if not held:
        return set()
    offsets = list(held)
    rows = cur.execute(
        "UPDATE event_log SET lease_expires_at = now() + make_interval(secs => %s)"
        " WHERE status = 'claimed' AND (\"offset\", lease_token) IN"
        " (SELECT * FROM unnest(%s::bigint[], %s::text[]))"
        ' RETURNING "offset"',
        (lease_seconds, offsets, [held[o] for o in offsets]),
    ).fetchall()
    return {int(r[0]) for r in rows}


def _transition(
    cur: psycopg.Cursor[Any],
    offset: int,
    lease: str | None,
    assignments: str,
    params: tuple[Any, ...] = (),
) -> bool:
    """Move a record on and release its lease. Given a lease token, only while that lease
    still holds the record: a worker whose lease ran out and was re-claimed changes nothing
    (A-105). Returns whether the record moved."""
    fence = " AND status = 'claimed' AND lease_token = %s" if lease is not None else ""
    cur.execute(
        f'UPDATE event_log SET {assignments}, {_RELEASE} WHERE "offset" = %s{fence}',
        (*params, offset, *(() if lease is None else (lease,))),
    )
    return cur.rowcount == 1


def mark_done(cur: psycopg.Cursor[Any], offset: int, *, lease: str | None) -> bool:
    return _transition(
        cur, offset, lease, "status = 'done', last_error = NULL, next_attempt_at = NULL"
    )


def mark_retry(
    cur: psycopg.Cursor[Any],
    offset: int,
    error: str,
    delay_seconds: float = 0.0,
    *,
    lease: str | None,
) -> bool:
    return _transition(
        cur,
        offset,
        lease,
        "status = 'pending', claimed_at = NULL, last_error = %s,"
        " next_attempt_at = now() + make_interval(secs => %s)",
        (error[:2000], delay_seconds),
    )


def mark_deferred(
    cur: psycopg.Cursor[Any], offset: int, reason: str, delay_seconds: float, *, lease: str | None
) -> bool:
    """A throttle: pending again after the wait, and the claim's attempt is given back."""
    return _transition(
        cur,
        offset,
        lease,
        "status = 'pending', claimed_at = NULL, last_error = %s,"
        " attempts = greatest(attempts - 1, 0), deferrals = deferrals + 1,"
        " next_attempt_at = now() + make_interval(secs => %s)",
        (reason[:2000], delay_seconds),
    )


def deferrals_of(cur: psycopg.Cursor[Any], offset: int) -> int:
    row = cur.execute('SELECT deferrals FROM event_log WHERE "offset" = %s', (offset,)).fetchone()
    return int(row[0]) if row else 0


def mark_parked(
    cur: psycopg.Cursor[Any],
    offset: int,
    reason: str,
    *,
    lease: str | None,
    give_back_attempt: bool = False,
) -> bool:
    """Not now: parked until someone unparks it. A park is not a failed try, so the claim's
    attempt can be given back."""
    refund = ", attempts = greatest(attempts - 1, 0)" if give_back_attempt else ""
    return _transition(
        cur,
        offset,
        lease,
        f"status = 'parked', claimed_at = NULL, last_error = %s{refund}",
        (reason[:2000],),
    )


def mark_dead(
    cur: psycopg.Cursor[Any], record: LogRecord, error: str, *, lease: str | None
) -> bool:
    moved = _transition(
        cur,
        record.offset,
        lease,
        "status = 'dead', last_error = %s, next_attempt_at = NULL",
        (error[:2000],),
    )
    if moved:
        cur.execute(  # a replayed record that dies again keeps its first dead-letter row
            'INSERT INTO dead_letter ("offset", tenant_id, topic, attempts, last_error)'
            ' VALUES (%s, %s, %s, %s, %s) ON CONFLICT ("offset") DO NOTHING',
            (record.offset, record.tenant_id, record.topic, record.attempts, error[:2000]),
        )
    return moved


def requeue_dead(cur: psycopg.Cursor[Any], offset: int) -> bool:
    """A dead parallel record goes back to pending with fresh attempts."""
    row = cur.execute(
        "UPDATE event_log SET status = 'pending', attempts = 0, deferrals = 0, claimed_at = NULL,"
        " next_attempt_at = NULL WHERE \"offset\" = %s AND status = 'dead'"
        " RETURNING tenant_id, topic",
        (offset,),
    ).fetchone()
    if row is not None:
        notify(cur, str(row[0]), str(row[1]))
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
