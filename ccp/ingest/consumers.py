"""Consumer base classes (spec.md Layer 2, A-48).

    OrderedConsumer.run_once                 ParallelConsumer.run_once
    ┌─ one transaction ──────────────┐       ┌─ short tx ─┐  ┌─ no tx ─┐  ┌─ short tx ─┐
    │ read cursor                    │       │ claim rows │→ │ handle  │→ │ done       │
    │ fetch rows after cursor        │       │ (SKIP      │  │ (model  │  │ retry      │
    │ sort by (source_ts, offset)    │       │  LOCKED,   │  │  calls  │  │ dead+DLQ   │
    │ handle each (entity writes)    │       │  attempts+1)│ │  here)  │  │ parked     │
    │ advance cursor                 │       └────────────┘  └─────────┘  └────────────┘
    └────────────────────────────────┘

A model call never runs inside a transaction; the parallel consumer's handle() runs
between two short transactions. A crashed worker's claim expires after the visibility
timeout and another worker re-claims the row; idempotent handlers make that safe.
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


class RetryableError(Exception):
    """Transient: the record goes back to pending until max_attempts, then dead-letter."""


class TerminalError(Exception):
    """Permanent: the record dead-letters on the first occurrence."""


class ParkedError(Exception):
    """Not now: the record is parked (tenant budget exhausted) and not counted as an attempt."""


class OrderedConsumer(ABC):
    name: str
    topic: str

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
        with self._db.transaction() as cur:
            last = event_log.read_cursor(cur, self.cursor_name, self.topic)
            records = event_log.fetch_after(cur, self.topic, last, batch_size)
            if not records:
                return 0
            for record in sorted(records, key=lambda r: (r.source_ts, r.offset)):
                self.handle(cur, record)
            event_log.advance_cursor(
                cur, self.cursor_name, self.topic, max(r.offset for r in records)
            )
        return len(records)


class ParallelConsumer(ABC):
    name: str
    topic: str

    def __init__(
        self, db: ScopedDB, *, max_attempts: int = 5, visibility_seconds: int = 300
    ) -> None:
        if kind_of(self.topic) is not TopicKind.parallel:
            raise TypeError(f"{self.topic!r} is not a parallel topic")
        self._db = db
        self.max_attempts = max_attempts
        self.visibility_seconds = visibility_seconds

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
        except Exception as exc:  # RetryableError and anything unclassified are retryable
            if record.attempts >= self.max_attempts:
                self._dead(record, f"gave up after {record.attempts} attempts: {exc}")
            else:
                with self._db.transaction() as cur:
                    event_log.mark_retry(cur, record.offset, str(exc))
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
