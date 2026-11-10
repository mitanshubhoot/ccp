"""Consumer base classes (spec.md Layer 2, A-48, A-97, A-104, A-105).

    OrderedConsumer.run_once                 ParallelConsumer.run_once
    ┌─ one transaction ──────────────┐       ┌─ short tx ─┐  ┌─ no tx ─┐  ┌─ short tx ─┐
    │ read cursor (a seq)            │       │ claim rows │→ │ handle  │→ │ done       │
    │ fetch rows after cursor        │       │ (SKIP      │  │ (model  │  │ retry+wait │
    │ sort by (source_ts, seq)       │       │  LOCKED,   │  │  calls  │  │ deferred   │
    │ handle each in a savepoint     │       │  a lease)  │  │  here)  │  │ dead+DLQ   │
    │ a failure: keep what came      │       └────────────┘  └─────────┘  │ parked     │
    │   before, count the try, stop  │        heartbeat: every third of   │ (fenced on │
    │ advance cursor to what is done │        a lease, renew what is held │  the token)│
    └────────────────────────────────┘                                    └────────────┘

A model call never runs inside a transaction; the parallel consumer's handle() runs
between two short transactions. A claim is a lease that a heartbeat thread renews while the
worker works through its batch, so a slow handler is never re-claimed under it; a crashed
worker's lease runs out and another worker re-claims the row; and a worker that lost its
lease cannot move the record afterwards. Idempotent handlers make the re-run safe.

On an ordered topic a failing record is retried after a backoff, holding the records
behind it so order is kept; after max_attempts (or at once for a terminal error) it is
dead-lettered for this consumer and the topic moves on. `replay` re-runs a dead record.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import psycopg

from ccp.ingest import event_log
from ccp.ingest.event_log import LogRecord
from ccp.ingest.topics import TopicKind, kind_of
from ccp.storage.scoped import ScopedDB

log = logging.getLogger(__name__)
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY = 2.0  # seconds; tests pass 0 to retry at once


class RetryableError(Exception):
    """Transient: the record goes back to pending after a backoff until max_attempts, then
    dead-letter."""


class ThrottledError(RetryableError):
    """The provider asked us to slow down (429, 529, 503): the record waits and the try is
    not counted against max_attempts."""


class TerminalError(Exception):
    """Permanent: the record dead-letters on the first occurrence."""


class ParkedError(Exception):
    """Not now: the record is parked (tenant budget exhausted) and not counted as an attempt."""


class OrderedConsumer(ABC):
    name: str
    topic: str
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay: float = DEFAULT_BASE_DELAY

    def __init__(self, db: ScopedDB) -> None:
        if kind_of(self.topic) is not TopicKind.ordered:
            raise TypeError(f"{self.topic!r} is not an ordered topic")
        self._db = db

    @property
    def cursor_name(self) -> str:
        return f"{self.name}:{self._db.tenant.tenant_id}"

    @abstractmethod
    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None: ...

    def run_once(self, batch_size: int = 100) -> int:
        """Handle the next batch in (source_ts, seq) order. Returns the records completed
        (handled, or skipped as dead); 0 when nothing is due, or when another worker is
        running this consumer for this tenant (A-106). The cursor is a seq (A-104)."""
        with self._db.transaction() as cur:
            if not event_log.try_lock(cur, f"ordered:{self.cursor_name}:{self.topic}"):
                return 0  # one worker at a time per (consumer, tenant): no shared batches
            last = event_log.read_cursor(cur, self.cursor_name, self.topic)
            records = event_log.fetch_after(cur, self.topic, last, batch_size)
            if not records:
                return 0
            failures = event_log.ordered_failures(cur, self.name, [r.offset for r in records])
            now = event_log.db_now(cur)
            ordered = sorted(records, key=lambda r: (r.source_ts, r.position))
            completed = 0
            for record in ordered:
                failure = failures.get(record.offset)
                if failure is not None and failure.status == "dead":
                    completed += 1
                    continue
                if (
                    failure is not None
                    and failure.status == "retrying"
                    and failure.next_attempt_at is not None
                    and failure.next_attempt_at > now
                ):
                    break  # backing off: the records behind it wait too, so order holds
                try:
                    with cur.connection.transaction():  # a savepoint: only this record undone
                        self.handle(cur, record)
                except Exception as exc:  # noqa: BLE001 - every failure is counted
                    counted = event_log.record_ordered_failure(
                        cur,
                        self.name,
                        record,
                        f"{type(exc).__name__}: {exc}",
                        terminal=isinstance(exc, TerminalError),
                        max_attempts=self.max_attempts,
                        base_delay=self.base_delay,
                    )
                    self._log_failure(record, counted)
                    if counted.status == "dead":
                        completed += 1
                        continue
                    break
                if failure is not None and failure.status == "retrying":
                    event_log.resolve_ordered_failure(cur, self.name, record.offset)
                completed += 1
            rest = ordered[completed:]
            cursor = min(r.position for r in rest) - 1 if rest else max(r.position for r in records)
            if cursor > last:
                event_log.advance_cursor(cur, self.cursor_name, self.topic, cursor)
        return completed

    def next_due(self) -> datetime | None:
        """When this consumer next has work for the tenant, once run_once returned 0: never
        (None) when it is caught up; else the end of a retry backoff, or now."""
        with self._db.transaction() as cur:
            last = event_log.read_cursor(cur, self.cursor_name, self.topic)
            if event_log.committed_seq(cur, self.topic) <= last:
                return None
            row = cur.execute(
                "SELECT min(next_attempt_at), now() FROM ordered_failures"
                " WHERE consumer = %s AND topic = %s AND status = 'retrying'",
                (self.name, self.topic),
            ).fetchone()
        assert row is not None
        return row[0] if row[0] is not None else row[1]  # type: ignore[no-any-return]

    def replay(self, offset: int, replayed_by: str) -> str:
        """Re-run a dead record for this consumer: 'done' when it now succeeds, 'failed'
        when it fails again (it stays dead). Every replay is recorded."""
        with self._db.transaction() as cur:
            failures = event_log.ordered_failures(cur, self.name, [offset])
            failure = failures.get(offset)
            if failure is None or failure.status != "dead":
                raise LookupError(f"offset {offset} is not dead-lettered for {self.name!r}")
            record = event_log.get(cur, offset)
            assert record is not None
            outcome, detail = "done", None
            try:
                with cur.connection.transaction():
                    self.handle(cur, record)
            except Exception as exc:  # noqa: BLE001 - the replay outcome records it
                outcome, detail = "failed", f"{type(exc).__name__}: {exc}"
            else:
                event_log.resolve_ordered_failure(cur, self.name, offset)
            event_log.record_replay(
                cur,
                tenant_id=record.tenant_id,
                topic=record.topic,
                offset=offset,
                outcome=outcome,
                replayed_by=replayed_by,
                consumer=self.name,
                detail=detail,
            )
        return outcome

    def _log_failure(self, record: LogRecord, failure: event_log.OrderedFailure) -> None:
        if failure.status == "dead":
            log.error(
                "DEAD LETTER %s/%s offset %s tenant %s after %s tries: %s",
                self.topic,
                self.name,
                record.offset,
                record.tenant_id,
                failure.attempts,
                failure.last_error,
            )
        else:
            log.warning(
                "%s offset %s try %s failed; retrying after a backoff: %s",
                self.name,
                record.offset,
                failure.attempts,
                failure.last_error,
            )


def worker_identity() -> str:
    """Who holds a lease, for operators reading event_log."""
    return f"{socket.gethostname()}:{os.getpid()}"


class LeaseKeeper:
    """Renews the leases a worker holds while it works through a claimed batch (A-105).

    A daemon thread renews every held lease each third of the lease length, in a transaction
    of its own. A lease that no longer renews was re-claimed by another worker after it ran
    out (the worker paused, or lost the database for longer than a lease); it is reported
    lost, and the record is neither handled nor moved by this worker afterwards."""

    def __init__(self, db: ScopedDB, records: list[LogRecord], lease_seconds: float) -> None:
        self._db = db
        self._lease_seconds = lease_seconds
        self._held = {r.offset: r.lease_token for r in records if r.lease_token is not None}
        self._lost: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name="lease-keeper", daemon=True)

    def __enter__(self) -> LeaseKeeper:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=max(self._lease_seconds, 5.0))

    def _beat(self) -> None:
        interval = max(self._lease_seconds / 3.0, 0.05)
        while not self._stop.wait(interval):
            self.renew()

    def renew(self) -> None:
        with self._lock:
            held = {o: t for o, t in self._held.items() if t is not None}
        if not held:
            return
        try:
            with self._db.transaction() as cur:
                kept = event_log.renew(cur, held, self._lease_seconds)
        except psycopg.Error as exc:  # the next beat tries again; the lease may still hold
            log.warning("lease renewal failed: %s", exc)
            return
        with self._lock:
            for offset in held:
                if offset not in kept and offset in self._held:
                    del self._held[offset]
                    self._lost.add(offset)

    def release(self, offset: int) -> None:
        with self._lock:
            self._held.pop(offset, None)

    def lost(self, offset: int) -> bool:
        with self._lock:
            return offset in self._lost


class ParallelConsumer(ABC):
    name: str
    topic: str

    def __init__(
        self,
        db: ScopedDB,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        visibility_seconds: float = 60,
        base_delay: float = DEFAULT_BASE_DELAY,
    ) -> None:
        if kind_of(self.topic) is not TopicKind.parallel:
            raise TypeError(f"{self.topic!r} is not a parallel topic")
        self._db = db
        self.max_attempts = max_attempts
        self.visibility_seconds = visibility_seconds  # the lease length (A-105)
        self.base_delay = base_delay
        self.owner = worker_identity()
        self.lost_leases = 0

    @abstractmethod
    def handle(self, record: LogRecord) -> None: ...

    def run_once(self, limit: int = 10) -> int:
        """Claim up to `limit` records and work through them under a heartbeat. Returns how
        many were claimed."""
        with self._db.transaction() as cur:
            records = event_log.claim(cur, self.topic, limit, self.visibility_seconds, self.owner)
        if not records:
            return 0
        with LeaseKeeper(self._db, records, self.visibility_seconds) as keeper:
            for record in records:
                if keeper.lost(record.offset):
                    self._lost(record, "before it was handled")
                    continue
                self._process(record)
                keeper.release(record.offset)
        return len(records)

    def next_due(self) -> datetime | None:
        """When a record of this topic next falls due for the tenant (A-106): a pending one
        now or when its backoff ends, a claimed one when its lease runs out; None when
        nothing is pending or claimed."""
        row = self._db.fetch_one(
            "SELECT min(CASE WHEN status = 'pending' THEN coalesce(next_attempt_at, now())"
            " ELSE coalesce(lease_expires_at, claimed_at + make_interval(secs => %s)) END)"
            " FROM event_log WHERE topic = %s AND status IN ('pending', 'claimed')",
            (self.visibility_seconds, self.topic),
        )
        return row[0] if row else None

    def _lost(self, record: LogRecord, when: str) -> None:
        self.lost_leases += 1
        log.warning(
            "%s offset %s: the lease was lost %s; another worker holds the record and this"
            " worker leaves it alone",
            self.name,
            record.offset,
            when,
        )

    def _process(self, record: LogRecord) -> None:
        lease = record.lease_token
        try:
            self.handle(record)
        except ParkedError as exc:
            with self._db.transaction() as cur:
                moved = event_log.mark_parked(
                    cur, record.offset, str(exc), lease=lease, give_back_attempt=True
                )
        except TerminalError as exc:
            moved = self._dead(record, f"terminal: {exc}")
        except ThrottledError as exc:
            with self._db.transaction() as cur:
                waited = event_log.deferrals_of(cur, record.offset)
                moved = event_log.mark_deferred(
                    cur,
                    record.offset,
                    f"throttled: {exc}",
                    event_log.backoff_seconds(waited + 1, self.base_delay),
                    lease=lease,
                )
            log.warning("%s offset %s throttled; deferred: %s", self.name, record.offset, exc)
        except Exception as exc:  # RetryableError and anything unclassified are retryable
            if record.attempts >= self.max_attempts:
                moved = self._dead(record, f"gave up after {record.attempts} attempts: {exc}")
            else:
                with self._db.transaction() as cur:
                    moved = event_log.mark_retry(
                        cur,
                        record.offset,
                        str(exc),
                        event_log.backoff_seconds(record.attempts, self.base_delay),
                        lease=lease,
                    )
                log.warning(
                    "%s offset %s attempt %s failed: %s",
                    self.name,
                    record.offset,
                    record.attempts,
                    exc,
                )
        else:
            with self._db.transaction() as cur:
                moved = event_log.mark_done(cur, record.offset, lease=lease)
        if not moved:
            self._lost(record, "while it was handled; its outcome is dropped")

    def _dead(self, record: LogRecord, error: str) -> bool:
        with self._db.transaction() as cur:
            moved = event_log.mark_dead(cur, record, error, lease=record.lease_token)
        if moved:
            log.error(
                "DEAD LETTER %s offset %s tenant %s: %s",
                self.topic,
                record.offset,
                record.tenant_id,
                error,
            )
        return moved
