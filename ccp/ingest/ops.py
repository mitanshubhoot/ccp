"""Operator actions on the queue (spec.md Layer 2, A-97): list dead letters, replay one, and
unpark parked records. Every replay is recorded in `dead_letter_replays`.

    dead_letters(db)                 parallel dead rows and ordered dead rows, per consumer
    replay(db, settings, offset, by) parallel: back to pending with fresh attempts;
                                     ordered: re-run by each consumer that dead-lettered it
    unpark(db, topic)                parked records (budget, AI tier unavailable) to pending
"""

from __future__ import annotations

from dataclasses import dataclass

from ccp.config import Settings
from ccp.ingest import event_log, worker
from ccp.ingest.consumers import OrderedConsumer
from ccp.ingest.topics import TopicKind, kind_of
from ccp.storage.scoped import ScopedDB


@dataclass(frozen=True)
class DeadLetter:
    offset: int
    topic: str
    consumer: str | None  # None on a parallel topic: the topic has one handler
    attempts: int
    error: str | None


def dead_letters(db: ScopedDB) -> list[DeadLetter]:
    with db.transaction() as cur:
        parallel = event_log.dead_parallel(cur)
        ordered = event_log.dead_ordered(cur)
    return sorted(
        [DeadLetter(r.offset, r.topic, None, r.attempts, r.last_error) for r in parallel]
        + [DeadLetter(f.offset, f.topic, f.consumer, f.attempts, f.last_error) for f in ordered],
        key=lambda d: (d.offset, d.consumer or ""),
    )


def _ordered_consumer(db: ScopedDB, settings: Settings, name: str, topic: str) -> OrderedConsumer:
    for factory in worker.CONSUMERS.values():
        candidate = factory(db, settings)
        if (
            isinstance(candidate, OrderedConsumer)
            and candidate.name == name
            and candidate.topic == topic
        ):
            return candidate
    raise LookupError(f"no registered consumer {name!r} on {topic!r}")


def replay(
    db: ScopedDB, settings: Settings, offset: int, replayed_by: str, consumer: str | None = None
) -> dict[str, str]:
    """Replay one dead record. Returns {consumer or topic: outcome}."""
    if not replayed_by.strip():
        raise ValueError("a name is required for --by")
    with db.transaction() as cur:
        record = event_log.get(cur, offset)
        if record is None:
            raise LookupError(f"no event_log row at offset {offset}")
        if kind_of(record.topic) is TopicKind.parallel:
            if not event_log.requeue_dead(cur, offset):
                raise LookupError(f"offset {offset} on {record.topic!r} is not dead")
            event_log.record_replay(
                cur,
                tenant_id=record.tenant_id,
                topic=record.topic,
                offset=offset,
                outcome="requeued",
                replayed_by=replayed_by.strip(),
            )
            return {record.topic: "requeued"}
        failures = [
            f
            for f in event_log.dead_ordered(cur)
            if f.offset == offset and (consumer is None or f.consumer == consumer)
        ]
    if not failures:
        raise LookupError(f"offset {offset} is not dead-lettered by any consumer")
    return {
        f.consumer: _ordered_consumer(db, settings, f.consumer, f.topic).replay(
            offset, replayed_by.strip()
        )
        for f in failures
    }


def unpark(db: ScopedDB, topic: str = "evaluations") -> int:
    """Parked records go back to pending; they park again if the cause still holds."""
    return db.execute(
        "UPDATE event_log SET status = 'pending', last_error = NULL, next_attempt_at = NULL"
        " WHERE topic = %s AND status = 'parked'",
        (topic,),
    )
