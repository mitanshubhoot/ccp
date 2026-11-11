"""The worker loop: one named consumer over many tenants, served fairly (spec.md Layer 2,
A-106).

The loop claims work by tenant through `work_hints` (ccp/ingest/dispatch.py): the ready
tenant served longest ago goes first and gets one quantum, so a backlog in one tenant cannot
starve another. It wakes on a database notification when a producer appends to its topic,
when the next hint falls due (a retry backoff ending, a lease running out), or for the
periodic sweep that re-checks every tenant; an idle tenant costs nothing in between. An
error in one tenant is logged, that tenant is tried again a little later, and the others go
on. Producers driven by the loop (the clock) run on sweeps only.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import psycopg

from ccp.config import Settings
from ccp.ingest import dispatch
from ccp.ingest.consumers import OrderedConsumer, ParallelConsumer
from ccp.ingest.event_log import CHANNEL, LogRecord
from ccp.observability import steps
from ccp.storage.db import app_pool, listen_url
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

log = logging.getLogger(__name__)


class Runnable(Protocol):
    """Anything the worker loop can drive: consumers and producers alike."""

    name: str
    topic: str

    def run_once(self) -> int: ...


ConsumerFactory = Callable[[ScopedDB, Settings], Runnable]
CONSUMERS: dict[str, ConsumerFactory] = {}


def register(name: str) -> Callable[[ConsumerFactory], ConsumerFactory]:
    def deco(factory: ConsumerFactory) -> ConsumerFactory:
        CONSUMERS[name] = factory
        return factory

    return deco


class RawAuditConsumer(OrderedConsumer):
    """Phase 2 demonstration consumer: logs each raw record as a step (normalize replaces it)."""

    name = "raw-audit"
    topic = "raw.github"

    def __init__(self, db: ScopedDB, topic: str) -> None:
        self.topic = topic
        super().__init__(db)

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        with steps.step(
            "raw.audit", layer="ingest", tenant=self._db.tenant, inputs={"offset": record.offset}
        ) as s:
            s.set_output({"partition_key": record.partition_key, "ref": record.ref})


for _topic in ("raw.github", "raw.hris", "raw.manual"):
    register(f"raw-audit:{_topic.split('.')[1]}")(
        lambda db, settings, t=_topic: RawAuditConsumer(db, t)  # type: ignore[misc]
    )


class Dispatcher:
    """One registered consumer over many tenants; see the module docstring."""

    def __init__(
        self,
        settings: Settings,
        consumer_name: str,
        tenants: Sequence[TenantContext],
        *,
        quantum: int = dispatch.QUANTUM_RUNS,
        hold_seconds: float = dispatch.HOLD_SECONDS,
        sweep_seconds: float = dispatch.SWEEP_SECONDS,
    ) -> None:
        try:
            self.factory = CONSUMERS[consumer_name]
        except KeyError:
            raise ValueError(
                f"unknown consumer {consumer_name!r}; known: {sorted(CONSUMERS)}"
            ) from None
        if not tenants:
            raise ValueError("no tenants to serve")
        self.settings = settings
        self.name = consumer_name
        self.tenants: dict[str, TenantContext] = {str(t.tenant_id): t for t in tenants}
        self.quantum = max(quantum, 1)
        self.hold_seconds = hold_seconds
        self.sweep_seconds = sweep_seconds
        self.processed = 0
        self.failures = 0
        self.pool = app_pool(settings)
        self.listener: psycopg.Connection[Any] | None = None
        try:
            sample = self.factory(ScopedDB(self.pool, tenants[0]), settings)
            self.topic = sample.topic
            if isinstance(sample, OrderedConsumer | ParallelConsumer):  # producers: sweeps only
                self.listener = psycopg.connect(listen_url(settings), autocommit=True)
                self.listener.execute(f"LISTEN {CHANNEL}")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        self.pool.close()

    def sweep(self) -> None:
        """Every tenant's hint ready now: at start, and the safety net every sweep."""
        with self.pool.connection() as conn:
            dispatch.mark_ready(conn, self.name, list(self.tenants))

    def wait(self, timeout: float) -> int:
        """Wait up to `timeout` for a notification; mark the tenants named ready. Returns
        how many tenants were woken."""
        if self.listener is None:
            time.sleep(timeout)
            return 0
        payloads = {n.payload for n in self.listener.notifies(timeout=timeout, stop_after=1)}
        payloads |= {n.payload for n in self.listener.notifies(timeout=0.01)}  # the rest
        woken = sorted(
            {
                tenant
                for tenant, _, topic in (p.partition(":") for p in payloads)
                if topic == self.topic and tenant in self.tenants
            }
        )
        if woken:
            with self.pool.connection() as conn:
                dispatch.mark_ready(conn, self.name, woken)
        return len(woken)

    def _serve(self, tenant: TenantContext) -> tuple[int, datetime | None]:
        """One quantum for one tenant: (records processed, when to look at it again)."""
        consumer = self.factory(ScopedDB(self.pool, tenant), self.settings)
        processed = 0
        for _ in range(self.quantum):
            n = consumer.run_once()
            processed += n
            if n == 0:
                break
        else:  # the quantum ran out with work left: ready now, behind every other tenant
            return processed, datetime.now(UTC)
        next_due = getattr(consumer, "next_due", None)
        due: datetime | None = next_due() if callable(next_due) else None
        now = datetime.now(UTC)
        if due is not None and due <= now:  # work it could not take now: look again soon
            due = now + timedelta(seconds=dispatch.IDLE_RETRY_SECONDS)
        return processed, due

    def serve_round(self, stop: threading.Event | None = None) -> int:
        """Serve every ready hint once, the one served longest ago first. Returns the
        records processed."""
        total = 0
        while stop is None or not stop.is_set():
            with self.pool.connection() as conn:
                claimed = dispatch.claim(conn, self.name, list(self.tenants), self.hold_seconds)
            if claimed is None:
                break
            tenant = self.tenants[claimed.tenant_id]
            again: datetime | None
            try:
                processed, again = self._serve(tenant)
            except Exception:  # one tenant's failure must not stop the others
                self.failures += 1
                log.exception("%s: tenant %s failed; trying again later", self.name, tenant)
                processed = 0
                again = datetime.now(UTC) + timedelta(seconds=dispatch.ERROR_RETRY_SECONDS)
            total += processed
            with self.pool.connection() as conn:
                dispatch.release(conn, self.name, claimed, again)
        self.processed += total
        return total

    def run(
        self,
        *,
        once: bool = True,
        poll_seconds: float = 2.0,
        max_loops: int | None = None,
        stop: threading.Event | None = None,
    ) -> int:
        """`once`: serve until no tenant is ready now, then return. Otherwise serve until
        `stop` is set (or `max_loops` rounds), waiting at most `poll_seconds` between checks
        when idle. Returns the records processed."""
        self.sweep()
        last_sweep = time.monotonic()
        loops = 0
        while stop is None or not stop.is_set():
            served = self.serve_round(stop)
            loops += 1
            if once and served == 0:
                break
            if max_loops is not None and loops >= max_loops:
                break
            if time.monotonic() - last_sweep >= self.sweep_seconds:
                self.sweep()
                last_sweep = time.monotonic()
                continue
            if served:
                continue
            with self.pool.connection() as conn:
                due = dispatch.seconds_to_next(conn, self.name, list(self.tenants))
            until_sweep = self.sweep_seconds - (time.monotonic() - last_sweep)
            wait = min(poll_seconds, until_sweep, poll_seconds if due is None else due)
            if wait > 0:
                self.wait(wait)
        return self.processed


def run(
    settings: Settings,
    consumer_name: str,
    tenants: Sequence[TenantContext],
    *,
    once: bool = True,
    poll_seconds: float = 2.0,
    max_loops: int | None = None,
    stop: threading.Event | None = None,
) -> int:
    """Run a consumer over the tenants. Returns the number of records processed."""
    dispatcher = Dispatcher(settings, consumer_name, tenants)
    try:
        return dispatcher.run(once=once, poll_seconds=poll_seconds, max_loops=max_loops, stop=stop)
    finally:
        dispatcher.close()
