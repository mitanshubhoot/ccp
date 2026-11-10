"""A listing seen whole settles every grant on it (spec.md A-102, A-119).

A live pull sees snapshots, not removal deliveries, and a snapshot's id comes from its content
(A-102), so a pull that sees nothing new lands no snapshot at all. What the pull saw is in its
last event, `org.pull.completed`, which is new every pull: the listings it saw whole (the
organization's members, and each repository's collaborators) and who was on them. Each such
listing settles every GitHub grant on that organization or repository at the pull time, as a
new observation, so the history keeps every span:

    on the listing,     grant active   -> still active at the pull time (presence)
    on the listing,     grant revoked  -> active again: added back, with a snapshot that may
                                          be one already landed, so nothing else says so
    not on the listing, grant active   -> revoked at the pull time
    not on the listing, grant revoked  -> nothing new

A grant already observed at or after the pull time is left alone, and a listing the pull did
not see whole proves nothing, whatever the other listings showed. Presence is what lets HR-1
and GH-1 see that access a terminated person still holds was still there after the deadline:
before, an unchanged grant stayed last observed at the pull that first saw it, so the checks
could only say needs_review.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg

from ccp.connectors.base import SourceEvent
from ccp.normalize.entities import AccessGrant
from ccp.normalize.github_mapper import asset_id, person_id
from ccp.types import GrantState

SETTLED_BY = "complete_pull"
# bookkeeping about the observation that wrote a row, never carried to the next one
_NOT_CARRIED = ("delivery", "inferred_from", "confirmed_by", "reappeared")


def listings(payload: dict[str, Any]) -> dict[str, set[str]]:
    """Each listing the pull saw whole: asset entity id -> the person entity ids on it. A
    marker from before A-119 has no `members_listed`; its `complete` stands in."""
    seen: dict[str, set[str]] = {}
    if payload.get("members_listed", payload.get("complete")) is True:
        org = asset_id(f"org:{payload['org']}")
        seen[org] = {person_id(n) for n in payload.get("members") or []}
    for node, nodes in (payload.get("repo_collaborators") or {}).items():
        seen[asset_id(node)] = {person_id(n) for n in nodes}
    return seen


def _attributes(value: Any) -> dict[str, Any]:
    loaded = json.loads(value) if isinstance(value, str) else (value or {})
    return {k: v for k, v in loaded.items() if k not in _NOT_CARRIED}


def _last_active(cur: psycopg.Cursor[Any], entity_id: str) -> tuple[str, dict[str, Any]]:
    """The role and attributes the grant last had while active."""
    row = cur.execute(
        "SELECT role, attributes FROM access_grants WHERE entity_id = %s AND state = 'active'"
        " ORDER BY observed_at DESC, source_event_id DESC LIMIT 1",
        (entity_id,),
    ).fetchone()
    return (str(row[0]), _attributes(row[1])) if row else ("member", {})


def from_complete_pull(cur: psycopg.Cursor[Any], event: SourceEvent) -> list[AccessGrant]:
    seen = listings(event.payload)
    if not seen:
        return []
    pulled = event.source_ts
    rows = cur.execute(
        "SELECT entity_id, person_entity_id, asset_entity_id, role, state, observed_at,"
        " state_effective_at, attributes FROM current_access_grants"
        " WHERE source_connector = 'github'"
    ).fetchall()
    out: list[AccessGrant] = []
    for entity_id, person, asset, role, state, observed_at, effective, attributes in rows:
        listed = seen.get(str(asset))
        if listed is None or observed_at >= pulled:
            continue  # not seen whole, or already observed by this pull or a later event
        on_it = str(person) in listed
        active = state == GrantState.active.value
        if active and on_it:  # still there
            new_role, new_state, since = str(role), GrantState.active, effective
            how = {**_attributes(attributes), "confirmed_by": SETTLED_BY}
        elif on_it:  # back on the listing after a revocation
            new_role, carried = _last_active(cur, str(entity_id))
            new_state, since = GrantState.active, pulled
            how = {**carried, "confirmed_by": SETTLED_BY, "reappeared": True}
        elif active:  # gone from the listing
            new_role, new_state, since = "revoked", GrantState.revoked, pulled
            how = {"inferred_from": SETTLED_BY}
        else:
            continue
        out.append(
            AccessGrant(
                tenant_id=event.tenant_id,
                entity_id=str(entity_id),
                source_connector="github",
                source_event_id=event.source_event_id,
                observed_at=pulled,
                person_entity_id=str(person),
                asset_entity_id=str(asset),
                role=new_role,
                state=new_state,
                state_effective_at=since,
                attributes=how,
            )
        )
    return out
