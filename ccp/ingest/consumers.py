"""Consumer base classes (spec.md Layer 2, A-48, A-97).

    OrderedConsumer.run_once                 ParallelConsumer.run_once
    ┌─ one transaction ──────────────┐       ┌─ short tx ─┐  ┌─ no tx ─┐  ┌─ short tx ─┐
    │ read cursor                    │       │ claim rows │→ │ handle  │→ │ done       │
    │ fetch rows after cursor        │       │ (SKIP      │  │ (model  │  │ retry+wait │
    │ sort by (source_ts, offset)    │       │  LOCKED,   │  │  calls  │  │ deferred   │
    │ handle each in a savepoint     │       │  attempts+1)│ │  here)  │  │ dead+DLQ   │
    │ a failure: keep what came      │       └────────────┘  └─────────┘  │ parked     │
    │   before, count the try, stop  │                                    └────────────┘
    │ advance cursor to what is done │
    └────────────────────────────────┘

A model call never runs inside a transaction; the parallel consumer's handle() runs
between two short transactions. A crashed worker's claim expires after the visibility
timeout and another worker re-claims the row; idempotent handlers make that safe.

On an ordered topic a failing record is retried after a backoff, holding the records
behind it so order is kept; after max_attempts (or at once for a terminal error) it is
dead-lettered for this consumer and the topic moves on. `replay` re-runs a dead record.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
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
        (handled, or skipped as dead); 0 when nothing is due. The cursor is a seq (A-104)."""
        with self._db.transaction() as cur:
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


class ParallelConsumer(ABC):
    name: str
    topic: str

    def __init__(
        self,
        db: ScopedDB,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        visibility_seconds: int = 300,
        base_delay: float = DEFAULT_BASE_DELAY,
    ) -> None:
        if kind_of(self.topic) is not TopicKind.parallel:
            raise TypeError(f"{self.topic!r} is not a parallel topic")
        self._db = db
        self.max_attempts = max_attempts
        self.visibility_seconds = visibility_seconds
        self.base_delay = base_delay

    @abstractmethod
    def handle(self, record: LogRecord) -> None: ...

    def run_once(self, limit: int = 10) -> int:
        with self._db.transaction() as cur:
            records = event_log.claim(cur, self.topic, limit, self.visibility_seconds)
        for record in records:
            self._process(record)
        return len(records)

    def _process(self, record: LogRecord) -> None:
        try:
            self.handle(record)
        except ParkedError as exc:
            with self._db.transaction() as cur:
                cur.execute(
                    'UPDATE event_log SET attempts = attempts - 1 WHERE "offset" = %s',
                    (record.offset,),
                )
                event_log.mark_parked(cur, record.offset, str(exc))
        except TerminalError as exc:
            self._dead(record, f"terminal: {exc}")
        except ThrottledError as exc:
            with self._db.transaction() as cur:
                waited = event_log.deferrals_of(cur, record.offset)
                event_log.mark_deferred(
                    cur,
                    record.offset,
                    f"throttled: {exc}",
                    event_log.backoff_seconds(waited + 1, self.base_delay),
                )
            log.warning("%s offset %s throttled; deferred: %s", self.name, record.offset, exc)
        except Exception as exc:  # RetryableError and anything unclassified are retryable
            if record.attempts >= self.max_attempts:
                self._dead(record, f"gave up after {record.attempts} attempts: {exc}")
            else:
                with self._db.transaction() as cur:
                    event_log.mark_retry(
                        cur,
                        record.offset,
                        str(exc),
                        event_log.backoff_seconds(record.attempts, self.base_delay),
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
                event_log.mark_done(cur, record.offset)

    def _dead(self, record: LogRecord, error: str) -> None:
        with self._db.transaction() as cur:
            event_log.mark_dead(cur, record, error)
        log.error(
            "DEAD LETTER %s offset %s tenant %s: %s",
            self.topic,
            record.offset,
            record.tenant_id,
            error,
        )
