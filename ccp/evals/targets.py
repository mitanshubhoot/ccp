"""Eval targets: what a golden case can invoke. Each returns a plain dict to compare.

Targets that need a database get one scratch database per eval run through
``EvalContext.db``; they must not touch the working database.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from ccp.config import Settings
from ccp.connectors.base import ConnectorName, SourceEvent, SourceKind, parse_ts
from ccp.connectors.registry import build_connector
from ccp.ingest import event_log
from ccp.ingest.consumers import OrderedConsumer
from ccp.ingest.event_log import LogRecord
from ccp.ingest.landing import land_events
from ccp.ingest.raw_zone import RawZone
from ccp.storage.db import app_pool
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext


@dataclass
class EvalContext:
    settings: Settings
    db_settings: Settings | None = None
    _pool: ConnectionPool | None = field(default=None, repr=False)

    def tenant(self, tenant_id: str | None) -> TenantContext:
        if tenant_id is None:
            raise ValueError("this target needs a tenant")
        return TenantContext.for_id(self.settings, tenant_id)

    def scoped(self, tenant_id: str | None) -> ScopedDB:
        if self.db_settings is None:
            raise RuntimeError("this target needs a database; run with a scratch database")
        if self._pool is None:
            self._pool = app_pool(self.db_settings, max_size=4)
        return ScopedDB(self._pool, self.tenant(tenant_id))

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None


TargetFn = Callable[[EvalContext, str | None, dict[str, Any]], dict[str, Any]]
TARGETS: dict[str, TargetFn] = {}


def target(name: str) -> Callable[[TargetFn], TargetFn]:
    def deco(fn: TargetFn) -> TargetFn:
        TARGETS[name] = fn
        return fn

    return deco


def _get_path(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


_CONNECTORS: tuple[ConnectorName, ...] = ("github", "hris", "manual")


def _connector_name(value: Any) -> ConnectorName:
    for name in _CONNECTORS:
        if value == name:
            return name
    raise ValueError(f"unknown connector {value!r}")


@target("connector_pull")
def connector_pull(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """Pull one connector: count, counts_by_type, and the events matching `where` (if given)."""
    source: SourceKind = "live" if inp.get("source") == "live" else "fixture"
    connector = build_connector(_connector_name(inp["connector"]), ctx.settings, source)
    since = parse_ts(inp["since"]) if inp.get("since") else None
    events = list(connector.pull(ctx.tenant(tenant_id), since))
    counts: dict[str, int] = {}
    for e in events:
        counts[e.event_type] = counts.get(e.event_type, 0) + 1
    where: dict[str, Any] = inp.get("where") or {}
    matches: list[dict[str, Any]] = []
    if where:
        matches = [
            {"event_type": e.event_type, "source_event_id": e.source_event_id, "payload": e.payload}
            for e in events
            if all(
                _get_path({"event_type": e.event_type, "payload": e.payload}, k) == v
                for k, v in where.items()
            )
        ]
    return {"count": len(events), "counts_by_type": counts, "matches": matches}


@target("ingest_land")
def ingest_land(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """Land one connector's events `times` times into a scratch database and count what happened."""
    db = ctx.scoped(tenant_id)
    zone = RawZone(ctx.settings.raw_dir / "eval" / db.tenant.tenant_id)
    connector = build_connector(_connector_name(inp["connector"]), ctx.settings, "fixture")
    landed_total = 0
    duplicates_total = 0
    for _ in range(int(inp.get("times", 1))):
        landed, duplicates = land_events(db, zone, connector.pull(db.tenant))
        landed_total += landed
        duplicates_total += duplicates
    raw_rows = db.fetch_one(
        "SELECT count(*) FROM raw_events WHERE connector = %s", (inp["connector"],)
    )
    log_rows = db.fetch_one(
        "SELECT count(*) FROM event_log WHERE topic = %s", (f"raw.{inp['connector']}",)
    )
    dup_files = len(list(zone.root.rglob("dups/*.json")))
    return {
        "landed": landed_total,
        "duplicates": duplicates_total,
        "raw_rows": int(raw_rows[0]) if raw_rows else 0,
        "log_records": int(log_rows[0]) if log_rows else 0,
        "dup_files": dup_files,
    }


class _RecordingConsumer(OrderedConsumer):
    name = "eval-recorder"
    topic = "raw.hris"

    def __init__(self, db: ScopedDB) -> None:
        super().__init__(db)
        self.handled: list[str] = []

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        self.handled.append(record.ref)


@target("ingest_ordering")
def ingest_ordering(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """Append synthetic records in the given order; report the order a consumer handled them."""
    db = ctx.scoped(tenant_id)
    with db.transaction() as cur:
        for rec in inp["records"]:
            event_log.append(
                cur,
                tenant_id=db.tenant.tenant_id,
                topic="raw.hris",
                partition_key=rec["partition"],
                source_ts=parse_ts(rec["source_ts"]),
                ref=rec["ref"],
            )
    consumer = _RecordingConsumer(db)
    while consumer.run_once():
        pass
    mine = {rec["ref"] for rec in inp["records"]}
    return {"handled_order": [ref for ref in consumer.handled if ref in mine]}


@target("mapper")
def mapper(ctx: EvalContext, tenant_id: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    """Map one fixture event (by connector and source_event_id) and report the entities produced."""
    from ccp.normalize.consumer import map_event

    connector = build_connector(_connector_name(inp["connector"]), ctx.settings, "fixture")
    wanted = inp["source_event_id"]
    event = next(
        (e for e in connector.pull(ctx.tenant(tenant_id)) if e.source_event_id == wanted), None
    )
    if event is None:
        raise LookupError(f"no fixture event {wanted!r}")
    result = map_event(event)
    entities = [
        {
            "kind": e.kind,
            "entity_id": e.entity_id,
            **{
                k: v
                for k, v in e.model_dump(mode="json").items()
                if k
                not in {
                    "tenant_id",
                    "source_connector",
                    "source_event_id",
                    "observed_at",
                    "entity_id",
                }
            },
        }
        for e in result.entities
    ]
    return {
        "event_type": event.event_type,
        "entities": entities,
        "guardrail_events": list(result.guardrail_events),
    }


def make_event(
    tenant: TenantContext, connector: ConnectorName, source_event_id: str, ts: datetime
) -> SourceEvent:
    return SourceEvent(
        tenant_id=tenant.tenant_id,
        connector=connector,
        source_event_id=source_event_id,
        source_ts=ts,
        event_type="synthetic",
        payload={"id": source_event_id},
        schema_version="synthetic.v1",
    )
