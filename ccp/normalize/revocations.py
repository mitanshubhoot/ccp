"""Revocations inferred from a complete pull (spec.md A-102).

A live pull sees snapshots, not removal deliveries, so a member who left the organization or a
collaborator removed from a repository simply stops appearing. Absence proves something only
when the pull saw every listing in full: `org.pull.completed` with `complete: true`. Then each
active GitHub grant on the organization, or on a repository whose collaborators were listed in
full, whose person the pull did not see becomes a revocation, written as a new observation at
the pull time, so the history keeps the span in which the grant was active. A repository the
pull could not list in full proves nothing about its collaborators.
"""

from __future__ import annotations

from typing import Any

import psycopg

from ccp.connectors.base import SourceEvent
from ccp.normalize.entities import AccessGrant
from ccp.normalize.github_mapper import asset_id, person_id
from ccp.types import GrantState


def from_complete_pull(cur: psycopg.Cursor[Any], event: SourceEvent) -> list[AccessGrant]:
    payload = event.payload
    if payload.get("complete") is not True:
        return []
    org_asset = asset_id(f"org:{payload['org']}")
    members = {person_id(node) for node in payload.get("members") or []}
    repos = {
        asset_id(node): {person_id(n) for n in nodes}
        for node, nodes in (payload.get("repo_collaborators") or {}).items()
    }
    rows = cur.execute(
        "SELECT entity_id, person_entity_id, asset_entity_id FROM current_access_grants"
        " WHERE source_connector = 'github' AND state = 'active'"
    ).fetchall()
    out: list[AccessGrant] = []
    for entity_id, person, asset in rows:
        if asset == org_asset:
            still_there = person in members
        elif asset in repos:
            still_there = person in repos[asset]
        else:
            continue
        if still_there:
            continue
        out.append(
            AccessGrant(
                tenant_id=event.tenant_id,
                entity_id=str(entity_id),
                source_connector="github",
                source_event_id=event.source_event_id,
                observed_at=event.source_ts,
                person_entity_id=str(person),
                asset_entity_id=str(asset),
                role="revoked",
                state=GrantState.revoked,
                state_effective_at=event.source_ts,
                attributes={"inferred_from": "complete_pull"},
            )
        )
    return out
