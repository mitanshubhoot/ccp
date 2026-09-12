"""Landing: connector events into the raw zone and the event log, with dedup.

for each SourceEvent
  ├── raw_zone.write(event)          file first; a second copy goes under dups/
  ├── INSERT raw_events ... ON CONFLICT DO NOTHING
  │     ├── inserted  -> append event_log(topic=raw.<connector>, ref=<raw path>)
  │     └── conflict  -> dedup_hit event; nothing else happens
  └── (one short transaction per event)
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ccp.config import Settings
from ccp.connectors.base import ConnectorName, SourceEvent, SourceKind
from ccp.connectors.registry import CONNECTOR_NAMES, build_connector
from ccp.ingest import event_log
from ccp.ingest.partition import partition_key
from ccp.ingest.raw_zone import RawZone
from ccp.observability import steps
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext


@dataclass
class LandingSummary:
    landed: int = 0
    duplicates: int = 0
    by_connector: dict[str, int] = field(default_factory=dict)

    def add(self, connector: str, landed: int, duplicates: int) -> None:
        self.landed += landed
        self.duplicates += duplicates
        self.by_connector[connector] = self.by_connector.get(connector, 0) + landed


def land_events(
    db: ScopedDB, zone: RawZone, events: Iterable[SourceEvent], step: steps.StepRecord | None = None
) -> tuple[int, int]:
    """Land a stream of events for one tenant. Returns (landed, duplicates)."""
    landed = 0
    duplicates = 0
    for event in events:
        if event.tenant_id != db.tenant.tenant_id:
            raise ValueError(
                f"event for {event.tenant_id!r} offered to {db.tenant.tenant_id!r}'s landing"
            )
        written = zone.write(event)
        with db.transaction() as cur:
            inserted = cur.execute(
                "INSERT INTO raw_events"
                " (tenant_id, connector, source_event_id, source_ts, raw_path, payload_sha256)"
                " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING source_event_id",
                (
                    event.tenant_id,
                    event.connector,
                    event.source_event_id,
                    event.source_ts,
                    str(written.path),
                    written.payload_sha256,
                ),
            ).fetchone()
            if inserted is None:
                duplicates += 1
                if step is not None:
                    step.event(f"dedup_hit:{event.connector}:{event.source_event_id}")
                continue
            event_log.append(
                cur,
                tenant_id=event.tenant_id,
                topic=f"raw.{event.connector}",
                partition_key=partition_key(event),
                source_ts=event.source_ts,
                ref=str(written.path),
            )
            landed += 1
    return landed, duplicates


def ingest_tenant(
    settings: Settings,
    db: ScopedDB,
    tenant: TenantContext,
    connectors: Sequence[ConnectorName] = CONNECTOR_NAMES,
    source: SourceKind = "fixture",
) -> LandingSummary:
    """Pull every requested connector for one tenant and land everything."""
    zone = RawZone(settings.raw_dir)
    summary = LandingSummary()
    for name in connectors:
        connector = build_connector(name, settings, source)
        with steps.step(
            "ingest.land",
            layer="ingest",
            tenant=tenant,
            inputs={"connector": name, "source": source},
        ) as s:
            landed, duplicates = land_events(db, zone, connector.pull(tenant), s)
            s.set_output({"landed": landed, "duplicates": duplicates})
        summary.add(name, landed, duplicates)
    return summary
