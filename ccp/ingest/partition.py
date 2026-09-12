"""Partition keys: tenant + connector + the subject the event is about.

Ordering guarantees hold within a partition. The subject is taken from the payload the
provider sent; when no subject field is recognisable the event id itself is the subject,
which makes the event its own partition.
"""

from __future__ import annotations

from typing import Any

from ccp.connectors.base import SourceEvent

_SUBJECT_FIELDS = (
    "node_id",
    "repo_node_id",
    "employee_id",
    "identity_id",
    "account_id",
    "asset_id",
    "filename",
)


def _delivery_subject(payload: dict[str, Any]) -> str | None:
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return None
    member = inner.get("member")
    if isinstance(member, dict) and member.get("node_id"):
        return str(member["node_id"])
    membership = inner.get("membership")
    if isinstance(membership, dict):
        user = membership.get("user")
        if isinstance(user, dict) and user.get("node_id"):
            return str(user["node_id"])
    return None


def subject_of(event: SourceEvent) -> str:
    payload = event.payload
    for field in _SUBJECT_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
    delivery = _delivery_subject(payload)
    if delivery:
        return delivery
    return event.source_event_id


def partition_key(event: SourceEvent) -> str:
    return f"{event.tenant_id}:{event.connector}:{subject_of(event)}"
