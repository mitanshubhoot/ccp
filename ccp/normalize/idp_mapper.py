"""Identity-provider snapshots to canonical entities (spec.md A-141). Pure.

idp.user.snapshot   the user as a person keyed by the provider's immutable id, with the
                    email identity resolution links to HR by; the directory as an asset;
                    and the user's sign-in as a grant on it: active unless the provider
                    deprovisioned or suspended them, with MFA (IDP-1). A terminated
                    employee linked to an active grant here fails HR-1 like any other.
"""

from __future__ import annotations

from typing import Any

from ccp.connectors.base import SourceEvent
from ccp.normalize.entities import AccessGrant, Asset, Entity, MappingResult, Person
from ccp.types import AccountKind, GrantState

ENDED = frozenset({"DEPROVISIONED", "SUSPENDED"})


def directory_asset_id(domain: str) -> str:
    return f"idp:asset:{domain}"


def _base(event: SourceEvent, entity_id: str) -> dict[str, Any]:
    return {
        "tenant_id": event.tenant_id,
        "entity_id": entity_id,
        "source_connector": "idp",
        "source_event_id": event.source_event_id,
        "observed_at": event.source_ts,
    }


def map_idp_event(event: SourceEvent) -> MappingResult:
    if event.event_type != "idp.user.snapshot":
        return MappingResult()
    p = event.payload
    person = f"idp:person:{p['user_id']}"
    directory = directory_asset_id(p["domain"])
    out: list[Entity] = [
        Asset(
            **_base(event, directory),
            name=f"Identity provider ({p['domain']})",
            asset_type="identity_provider",
            tags=("critical",),
            settings={"domain": p["domain"]},
        ),
        Person(
            **_base(event, person),
            display_name=p.get("display_name") or p.get("login"),
            emails=tuple(e for e in (p.get("email"),) if e),
            handles={"okta": p["login"]} if p.get("login") else {},
            account_kind=AccountKind.human,
        ),
        AccessGrant(
            **_base(event, f"idp:grant:{p['user_id']}"),
            person_entity_id=person,
            asset_entity_id=directory,
            role="sso",
            state=GrantState.revoked if p.get("status") in ENDED else GrantState.active,
            state_effective_at=event.source_ts,
            attributes={
                "source": "idp",
                "user_id": p["user_id"],
                "status": p.get("status"),
                "email": p.get("email"),
                "mfa_enabled": bool(p.get("mfa_enabled")),
                "factors": list(p.get("factors") or []),
                "kind": "human",
            },
        ),
    ]
    return MappingResult(entities=tuple(out))
