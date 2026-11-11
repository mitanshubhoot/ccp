"""The lookup table (spec.md A-158): what code reads about a failing resource, per test.

Reference data like the action catalogue: code chooses every read from the failing verdict
and this table, so no model picks what is read or with which parameters. The table is not
part of the catalogue's version (A-128) because it changes what is read about a failure,
never what is tested or how a verdict is decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

from ccp.knowledge.graph import KNOWLEDGE_DIR

LOOKUPS_FILE = KNOWLEDGE_DIR / "lookups.yaml"
LookupKind = Literal["config_history", "cloudtrail", "infra_code"]
LOOKUP_KINDS: tuple[LookupKind, ...] = ("config_history", "cloudtrail", "infra_code")


@dataclass(frozen=True)
class TrailLookup:
    by: Literal["resource_name", "event_source"]
    events: tuple[str, ...]
    source: str | None = None  # the event source, when looked up by it


@dataclass(frozen=True)
class Plan:
    test_id: str
    connector: str
    config_history: str | None  # an AWS Config resource type
    cloudtrail: TrailLookup | None
    infra_code: tuple[str, ...]  # Terraform resource types
    infra_search: Literal["name", "type"] = "name"

    @property
    def kinds(self) -> tuple[LookupKind, ...]:
        out: list[LookupKind] = []
        if self.config_history:
            out.append("config_history")
        if self.cloudtrail is not None:
            out.append("cloudtrail")
        if self.infra_code:
            out.append("infra_code")
        return tuple(out)


def _plan(test_id: str, raw: dict[str, object]) -> Plan:
    unknown = set(raw) - {"connector", "config_history", "cloudtrail", "infra_code", "infra_search"}
    if unknown:
        raise ValueError(f"lookups.yaml {test_id}: unknown keys {sorted(unknown)}")
    trail = raw.get("cloudtrail")
    lookup: TrailLookup | None = None
    if isinstance(trail, dict):
        by = trail.get("by")
        if by not in ("resource_name", "event_source"):
            raise ValueError(f"lookups.yaml {test_id}: cloudtrail.by is {by!r}")
        if by == "event_source" and not trail.get("source"):
            raise ValueError(f"lookups.yaml {test_id}: a lookup by event source names it")
        lookup = TrailLookup(
            by, tuple(str(e) for e in trail.get("events") or ()), trail.get("source")
        )
    codes = raw.get("infra_code") or []
    if not isinstance(codes, list):
        raise ValueError(f"lookups.yaml {test_id}: infra_code is a list of resource types")
    history = raw.get("config_history")
    search = raw.get("infra_search") or "name"
    if search not in ("name", "type"):
        raise ValueError(f"lookups.yaml {test_id}: infra_search is name or type")
    return Plan(
        test_id,
        str(raw.get("connector") or ""),
        str(history) if history else None,
        lookup,
        tuple(str(c) for c in codes),
        "type" if search == "type" else "name",
    )


@lru_cache(maxsize=2)
def load(path: Path = LOOKUPS_FILE) -> dict[str, Plan]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(test_id): _plan(str(test_id), body) for test_id, body in raw.items()}


def plan_for(test_id: str) -> Plan | None:
    return load().get(test_id)
