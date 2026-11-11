"""The normalize consumers: raw.<connector> topic -> mapper -> entity tables -> entities topic.

One ordered consumer per raw topic. Each record's raw file is read back from the raw zone,
mapped by the connector's pure mapper, persisted (idempotently), and announced on the
``entities`` topic with the entity id as the partition key. Guardrail events raised by a
mapper are recorded on the step.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg

from ccp.config import Settings
from ccp.connectors.base import SourceEvent
from ccp.ingest import event_log, raw_zone
from ccp.ingest.consumers import OrderedConsumer
from ccp.ingest.event_log import LogRecord
from ccp.ingest.worker import register
from ccp.normalize import repository, revocations
from ccp.normalize.aws_mapper import map_aws_event
from ccp.normalize.entities import MappingResult
from ccp.normalize.github_mapper import map_github_event
from ccp.normalize.hris_mapper import map_hris_event
from ccp.normalize.idp_mapper import map_idp_event
from ccp.normalize.manual_mapper import map_manual_event
from ccp.observability import steps
from ccp.storage.scoped import ScopedDB

Mapper = Callable[[SourceEvent], MappingResult]
MAPPERS: dict[str, Mapper] = {
    "github": map_github_event,
    "hris": map_hris_event,
    "manual": map_manual_event,
    "aws": map_aws_event,
    "idp": map_idp_event,
}
RAW_CONNECTORS = tuple(MAPPERS)  # one raw topic, and one normalize consumer, each


def map_event(event: SourceEvent) -> MappingResult:
    return MAPPERS[event.connector](event)


class NormalizeConsumer(OrderedConsumer):
    name = "normalize"

    def __init__(self, db: ScopedDB, connector: str) -> None:
        self.topic = f"raw.{connector}"
        self.connector = connector
        super().__init__(db)

    def handle(self, cur: psycopg.Cursor[Any], record: LogRecord) -> None:
        # a ref is a local path or an S3 version (A-136); a local one may have gone missing
        if not record.ref.startswith("s3://") and not Path(record.ref).is_file():
            # An ordered consumer must never be wedged by one bad record: log and move on.
            with steps.step(
                "normalize.map",
                layer="normalize",
                tenant=self._db.tenant,
                inputs={"offset": record.offset, "ref": record.ref},
            ) as s:
                s.event(f"raw_file_missing:{record.offset}")
                s.set_output({"entities": 0, "inserted": 0})
            return
        event = raw_zone.read_ref(record.ref)
        with steps.step(
            "normalize.map",
            layer="normalize",
            tenant=self._db.tenant,
            inputs={"offset": record.offset, "event_type": event.event_type},
        ) as s:
            result = map_event(event)
            entities = list(result.entities)
            if event.event_type == "org.pull.completed":  # settle its listings (A-119)
                entities += revocations.from_complete_pull(cur, event)
            inserted = 0
            for entity in entities:
                if repository.insert(cur, entity):
                    inserted += 1
                    event_log.append(
                        cur,
                        tenant_id=entity.tenant_id,
                        topic="entities",
                        partition_key=f"{entity.tenant_id}:{entity.entity_id}",
                        source_ts=entity.observed_at,
                        ref=f"{repository.table_for(entity)}:{entity.entity_id}:{entity.source_event_id}",
                    )
            for guardrail in result.guardrail_events:
                s.event(guardrail)
            s.set_output({"entities": len(entities), "inserted": inserted})


for _connector in RAW_CONNECTORS:
    register(f"normalize:{_connector}")(
        lambda db, settings, c=_connector: NormalizeConsumer(db, c)  # type: ignore[misc]
    )


def normalize_all(db: ScopedDB, settings: Settings) -> int:
    """Drain every raw topic for one tenant. Returns records processed."""
    total = 0
    for connector in RAW_CONNECTORS:
        consumer = NormalizeConsumer(db, connector)
        while True:
            n = consumer.run_once()
            total += n
            if n == 0:
                break
    return total
