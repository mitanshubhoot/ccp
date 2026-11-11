"""Landing: connector events into the raw zone and the event log, with dedup.

for each SourceEvent
  ├── raw_zone.write(event)          file first; a second copy goes under dups/
  ├── INSERT raw_events ... ON CONFLICT DO NOTHING
  │     ├── inserted  -> append event_log(topic=raw.<connector>, ref=<raw path>)
  │     └── conflict  -> dedup_hit event; nothing else happens
  └── (one short transaction per event)

Every pull is a sync run (A-118): `sync_connector` lands one connector's pull, records how
each of its scopes ended, and turns an exception into a failed run instead of an abort, so
one connector's failure no longer stops the connectors after it or goes unrecorded.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ulid import ULID

from ccp.config import Settings
from ccp.connectors.base import (
    Connection,
    Connector,
    ConnectorName,
    PullError,
    PullErrorKind,
    Received,
    SourceEvent,
    SourceKind,
    SyncReport,
)
from ccp.connectors.registry import CONNECTOR_NAMES, build_connector
from ccp.ingest import connections, event_log, sync_runs
from ccp.ingest.partition import partition_key
from ccp.ingest.raw_zone import RawStore, open_zone
from ccp.ingest.sync_runs import SyncRun
from ccp.observability import steps
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext


@dataclass
class Tally:
    landed: int = 0
    duplicates: int = 0


@dataclass
class LandingSummary:
    landed: int = 0
    duplicates: int = 0
    by_connector: dict[str, int] = field(default_factory=dict)
    runs: list[SyncRun] = field(default_factory=list)

    def add(self, run: SyncRun) -> None:
        self.landed += run.landed
        self.duplicates += run.duplicates
        self.by_connector[run.connector] = self.by_connector.get(run.connector, 0) + run.landed
        self.runs.append(run)

    @property
    def failed(self) -> list[SyncRun]:
        return [r for r in self.runs if r.status == "failed"]


def land_events(
    db: ScopedDB,
    zone: RawStore,
    events: Iterable[SourceEvent],
    step: steps.StepRecord | None = None,
    tally: Tally | None = None,
) -> tuple[int, int]:
    """Land a stream of events for one tenant. Returns (landed, duplicates); `tally` keeps
    the running counts, so a stream that raises part way still reports what it landed."""
    count = tally if tally is not None else Tally()
    for event in events:
        if event.tenant_id != db.tenant.tenant_id:
            raise ValueError(
                f"event for {event.tenant_id!r} offered to {db.tenant.tenant_id!r}'s landing"
            )
        written = zone.write(event)
        with db.transaction() as cur:
            inserted = cur.execute(
                "INSERT INTO raw_events"
                " (tenant_id, connector, source_event_id, source_ts, raw_path, payload_sha256,"
                " stored_sha256, version_id, store)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT DO NOTHING RETURNING source_event_id",
                (
                    event.tenant_id,
                    event.connector,
                    event.source_event_id,
                    event.source_ts,
                    written.ref,
                    written.payload_sha256,
                    written.stored_sha256,
                    written.version_id,
                    written.store,
                ),
            ).fetchone()
            if inserted is None:
                count.duplicates += 1
                if step is not None:
                    step.event(f"dedup_hit:{event.connector}:{event.source_event_id}")
                continue
            event_log.append(
                cur,
                tenant_id=event.tenant_id,
                topic=f"raw.{event.connector}",
                partition_key=partition_key(event),
                source_ts=event.source_ts,
                ref=written.ref,
            )
            count.landed += 1
    return count.landed, count.duplicates


def sync_connector(
    settings: Settings,
    db: ScopedDB,
    tenant: TenantContext,
    connector: Connector,
    *,
    zone: RawStore | None = None,
    connection_id: str | None = None,
) -> SyncRun:
    """One connector's pull, landed and recorded as a sync run (A-118). Never raises for the
    pull's own failures: they end the run as failed, with the error's class."""
    report = SyncReport(connector.scopes)
    kind: PullErrorKind | None = None
    tally = Tally()
    started = datetime.now(UTC)
    with steps.step(
        "ingest.land",
        layer="ingest",
        tenant=tenant,
        inputs={"connector": connector.name, "source": connector.source},
    ) as s:
        try:
            land_events(
                db,
                zone or open_zone(settings),
                connector.pull(tenant, None, report),
                s,
                tally,
            )
        except Exception as exc:  # this connector's run fails; the others still run
            report.error = f"{type(exc).__name__}: {exc}"
            kind = _kind(exc)
            s.event(f"sync_failed:{connector.name}:{kind}:{type(exc).__name__}")
        run = SyncRun(
            run_id=sync_runs.new_run_id(),
            connector=connector.name,
            source=connector.source,
            status=report.status(),
            scopes=report.outcomes(),
            landed=tally.landed,
            duplicates=tally.duplicates,
            started_at=started,
            finished_at=datetime.now(UTC),
            error=report.error,
            error_kind=kind,
        )
        sync_runs.record(db, run, connection_id)
        kept = land_responses(db, zone or open_zone(settings), connector, run.run_id)
        if kept:
            s.event(f"responses_kept:{kept}")
        s.set_output(
            {
                "status": run.status,
                "landed": run.landed,
                "duplicates": run.duplicates,
                "scopes": {k: v.status for k, v in run.scopes.items()},
            }
        )
    return run


def land_responses(db: ScopedDB, zone: RawStore, connector: Connector, run_id: str) -> int:
    """Keep every response the pull received, as the provider sent it (A-136): its bytes in
    the raw zone, content-addressed, and a row naming the request id and the bytes' digest."""
    received: list[Received] = list(getattr(connector, "responses", ()))
    return len(land_received(db, zone, connector.name, received, run_id))


def land_received(
    db: ScopedDB, zone: RawStore, connector: str, received: list[Received], run_id: str
) -> list[str]:
    """Land responses as raw evidence and return their raw_responses ids, in order: a pull's
    (A-136), or a lookup's about one failing resource (A-158)."""
    ids: list[str] = []
    for item in received:
        landed = zone.put_response(db.tenant.tenant_id, connector, item)
        response_id = str(ULID())
        db.execute(
            "INSERT INTO raw_responses (tenant_id, response_id, connector, sync_run_id, method,"
            " path, status, request_id, body_sha256, ref, version_id, store, received_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                db.tenant.tenant_id,
                response_id,
                connector,
                run_id,
                item.method,
                item.path,
                item.status,
                item.request_id,
                item.sha256,
                landed.ref,
                landed.version_id,
                landed.store,
                item.received_at,
            ),
        )
        ids.append(response_id)
    return ids


def _kind(exc: Exception) -> PullErrorKind:
    """Why a pull stopped, in the error table's terms (A-120); anything unnamed is a fail."""
    return exc.kind if isinstance(exc, PullError) else "fail"


def _failed_to_start(
    db: ScopedDB, name: str, source: str, exc: Exception, connection_id: str | None = None
) -> SyncRun:
    """A connector that could not even be built (no credential for a live source): a failed
    run with no scopes, recorded like any other."""
    now = datetime.now(UTC)
    run = SyncRun(
        run_id=sync_runs.new_run_id(),
        connector=name,
        source=source,
        status="failed",
        scopes={},
        landed=0,
        duplicates=0,
        started_at=now,
        finished_at=now,
        error=f"{type(exc).__name__}: {exc}",
        error_kind=_kind(exc),
    )
    sync_runs.record(db, run, connection_id)
    return run


def ingest_tenant(
    settings: Settings,
    db: ScopedDB,
    tenant: TenantContext,
    connectors: Sequence[ConnectorName] = CONNECTOR_NAMES,
    source: SourceKind = "fixture",
) -> LandingSummary:
    """Pull every requested connector for one tenant, each its own recorded sync run, under
    its declared connection when there is one (A-121); a pull with none is recorded too."""
    connections.sync(db)
    zone = open_zone(settings)
    summary = LandingSummary()
    for name in connectors:
        connection = connections.for_pull(db, name, source)
        summary.add(_pull(settings, db, tenant, name, source, connection, zone))
    return summary


def sync_connections(settings: Settings, db: ScopedDB, tenant: TenantContext) -> LandingSummary:
    """The scheduled sync (A-121): every enabled connection pulled, each its own run."""
    connections.sync(db)
    zone = open_zone(settings)
    summary = LandingSummary()
    for connection in connections.enabled(db):
        summary.add(
            _pull(settings, db, tenant, connection.connector, connection.source, connection, zone)
        )
    return summary


def _pull(
    settings: Settings,
    db: ScopedDB,
    tenant: TenantContext,
    name: ConnectorName,
    source: SourceKind,
    connection: Connection | None,
    zone: RawStore,
) -> SyncRun:
    cid = connection.connection_id if connection else None
    try:
        connector = build_connector(name, settings, source, connection)
    except Exception as exc:  # the next connector still runs
        return _failed_to_start(db, name, source, exc, cid)
    return sync_connector(settings, db, tenant, connector, zone=zone, connection_id=cid)
