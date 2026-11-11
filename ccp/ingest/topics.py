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
    "raw.aws": TopicKind.ordered,  # A-140
    "raw.idp": TopicKind.ordered,  # A-141
    "entities": TopicKind.ordered,
    "evaluations": TopicKind.parallel,
    "investigations": TopicKind.parallel,
    "jobs": TopicKind.parallel,  # scheduled jobs (A-108)
    "applies": TopicKind.parallel,  # recorded decisions to apply (A-110)
    "task_items": TopicKind.parallel,  # a long task's fanned-out reads (A-151)
    "lookups": TopicKind.parallel,  # reads about a failing resource (A-158)
    "adequacy": TopicKind.parallel,  # the adequacy verifier on an upload (A-162)
    "verdicts": TopicKind.ordered,
}


def kind_of(topic: str) -> TopicKind:
    try:
        return TOPICS[topic]
    except KeyError:
        raise ValueError(f"unknown topic {topic!r}; declare it in ccp/ingest/topics.py") from None
