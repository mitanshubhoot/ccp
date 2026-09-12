"""Pure observation merging, shared by the repository reads and in-memory tests.

Persons merge per field in observed_at order; a null, empty, or unknown value never
overrides a known one, so a partial HR event cannot blank out a full snapshot. Asset
settings merge per key. Grants, evidence, and vendors take the newest observation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

from ccp.normalize.entities import Asset, CanonicalBase, Person
from ccp.types import AccountKind, EmploymentStatus

T = TypeVar("T", bound=CanonicalBase)


def _ordered(observations: Iterable[T]) -> list[T]:
    return sorted(observations, key=lambda o: (o.entity_id, o.observed_at, o.source_event_id))


def merge_persons(observations: Iterable[Person]) -> list[Person]:
    merged: dict[str, dict[str, Any]] = {}
    for obs in _ordered(observations):
        cur = merged.setdefault(
            obs.entity_id,
            {
                "tenant_id": obs.tenant_id,
                "entity_id": obs.entity_id,
                "display_name": None,
                "emails": (),
                "handles": {},
                "employee_id": None,
                "account_kind": AccountKind.unknown,
                "employment_status": EmploymentStatus.unknown,
                "status_effective_at": None,
                "owner_employee_id": None,
            },
        )
        cur["observed_at"] = obs.observed_at
        cur["source_connector"] = obs.source_connector
        cur["source_event_id"] = obs.source_event_id
        if obs.display_name:
            cur["display_name"] = obs.display_name
        if obs.emails:
            cur["emails"] = obs.emails
        if obs.handles:
            cur["handles"] = {**cur["handles"], **obs.handles}
        if obs.employee_id:
            cur["employee_id"] = obs.employee_id
        if obs.account_kind is not AccountKind.unknown:
            cur["account_kind"] = obs.account_kind
        if obs.employment_status is not EmploymentStatus.unknown:
            cur["employment_status"] = obs.employment_status
            cur["status_effective_at"] = obs.status_effective_at
        if obs.owner_employee_id:
            cur["owner_employee_id"] = obs.owner_employee_id
    return [Person(**merged[k]) for k in sorted(merged)]


def merge_assets(observations: Iterable[Asset]) -> list[Asset]:
    merged: dict[str, Asset] = {}
    settings: dict[str, dict[str, Any]] = {}
    for obs in _ordered(observations):
        settings.setdefault(obs.entity_id, {}).update(obs.settings)
        merged[obs.entity_id] = obs.model_copy(update={"settings": dict(settings[obs.entity_id])})
    return [merged[k] for k in sorted(merged)]


def latest(observations: Iterable[T]) -> list[T]:
    newest: dict[str, T] = {}
    for obs in _ordered(observations):
        newest[obs.entity_id] = obs
    return [newest[k] for k in sorted(newest)]
