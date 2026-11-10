"""Topic registry: every topic declares its kind, and the kind decides the consumer class.

ordered   one consumer per topic, a seq cursor (A-104), batch sorted by source_ts;
          at-least-once, consumers idempotent; no model calls
parallel  N consumers, per-row claiming with SKIP LOCKED, attempts, a lease renewed by a
          heartbeat (A-105), dead-letter after max_attempts; idempotent by evaluation key
"""

from __future__ import annotations

from enum import StrEnum


class TopicKind(StrEnum):
    ordered = "ordered"
    parallel = "parallel"


TOPICS: dict[str, TopicKind] = {
    "raw.github": TopicKind.ordered,
    "raw.hris": TopicKind.ordered,
    "raw.manual": TopicKind.ordered,
    "entities": TopicKind.ordered,
    "evaluations": TopicKind.parallel,
    "investigations": TopicKind.parallel,
    "verdicts": TopicKind.ordered,
}


def kind_of(topic: str) -> TopicKind:
    try:
        return TOPICS[topic]
    except KeyError:
        raise ValueError(f"unknown topic {topic!r}; declare it in ccp/ingest/topics.py") from None
