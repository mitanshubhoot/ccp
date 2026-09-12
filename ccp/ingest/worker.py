"""The worker loop: run a named consumer for one or more tenants, once or polling."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

import psycopg

from ccp.config import Settings
from ccp.ingest.consumers import OrderedConsumer, ParallelConsumer
from ccp.ingest.event_log import LogRecord
from ccp.observability import steps
from ccp.storage.db import app_pool
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

log = logging.getLogger(__name__)

ConsumerFactory = Callable[[ScopedDB, Settings], OrderedConsumer | ParallelConsumer]
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


def run(
    settings: Settings,
    consumer_name: str,
    tenants: Sequence[TenantContext],
    *,
    once: bool = True,
    poll_seconds: float = 2.0,
    max_loops: int | None = None,
) -> int:
    """Run a consumer for each tenant. Returns the number of records processed."""
    try:
        factory = CONSUMERS[consumer_name]
    except KeyError:
        raise ValueError(
            f"unknown consumer {consumer_name!r}; known: {sorted(CONSUMERS)}"
        ) from None
    pool = app_pool(settings)
    total = 0
    loops = 0
    try:
        while True:
            processed = 0
            for tenant in tenants:
                consumer = factory(ScopedDB(pool, tenant), settings)
                while True:
                    n = consumer.run_once()
                    processed += n
                    if n == 0:
                        break
            total += processed
            loops += 1
            if once or (max_loops is not None and loops >= max_loops):
                return total
            if processed == 0:
                time.sleep(poll_seconds)
    finally:
        pool.close()
