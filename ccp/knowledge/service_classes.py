"""Service classes, their intents, the reviewed type lookup and the coverage table
(spec.md A-166), and a person's approval of a class for a new type."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import yaml

from ccp.knowledge.graph import KNOWLEDGE_DIR

CLASSES_FILE = KNOWLEDGE_DIR / "service_classes.yaml"
REVIEW_LOG = "class_reviews.jsonl"
_TYPE = re.compile(r"^[A-Za-z0-9]+::[A-Za-z0-9]+::[A-Za-z0-9]+$")
# what a type's name suggests, for a proposal only: a person approves every class
_HINTS: tuple[tuple[str, str], ...] = (
    ("Bucket", "data_store"),
    ("Table", "data_store"),
    ("DB", "data_store"),
    ("Cluster", "data_store"),
    ("Key", "keys_secrets"),
    ("Secret", "keys_secrets"),
    ("Certificate", "keys_secrets"),
    ("User", "identity"),
    ("Role", "identity"),
    ("Group", "identity"),
    ("Function", "serverless"),
    ("Instance", "compute"),
    ("Queue", "messaging"),
    ("Topic", "messaging"),
    ("Stream", "messaging"),
    ("LoadBalancer", "network_edge"),
    ("Distribution", "network_edge"),
    ("Api", "network_edge"),
    ("Trail", "logging"),
    ("LogGroup", "logging"),
    ("Repository", "container"),
    ("Model", "ai_ml"),
    ("Endpoint", "ai_ml"),
)


@dataclass(frozen=True)
class Classes:
    intents: dict[str, tuple[str, ...]]
    types: dict[str, str]
    coverage: dict[str, dict[str, str]]

    def class_of(self, resource_type: str) -> str | None:
        return self.types.get(resource_type)

    def covering_test(self, resource_type: str, intent: str) -> str | None:
        return self.coverage.get(resource_type, {}).get(intent)


@lru_cache(maxsize=4)
def load(path: Path = CLASSES_FILE) -> Classes:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    intents = {str(k): tuple(str(i) for i in v) for k, v in (raw.get("classes") or {}).items()}
    types = {str(k): str(v) for k, v in (raw.get("types") or {}).items()}
    for resource_type, cls in types.items():
        if cls not in intents:
            raise ValueError(f"{resource_type}: unknown class {cls!r}")
    coverage = {
        str(t): {str(i): str(test) for i, test in (m or {}).items()}
        for t, m in (raw.get("coverage") or {}).items()
    }
    for resource_type, by_intent in coverage.items():
        owner = types.get(resource_type)
        if owner is None:
            raise ValueError(f"coverage for unclassified {resource_type}")
        unknown = set(by_intent) - set(intents[owner])
        if unknown:
            raise ValueError(f"{resource_type}: intents {sorted(unknown)} are not {owner}'s")
    return Classes(intents, types, coverage)


def propose_class(resource_type: str, classes: Classes | None = None) -> str | None:
    """A class a type's name suggests, for a person to approve; None when nothing does."""
    found = classes or load()
    if resource_type in found.types:
        return found.types[resource_type]
    name = resource_type.rsplit("::", 1)[-1]
    for hint, cls in _HINTS:
        if hint in name and cls in found.intents:
            return cls
    return None


def approve(
    resource_type: str,
    cls: str,
    reviewer: str,
    path: Path = CLASSES_FILE,
) -> dict[str, str]:
    """A person approves a class for a new type: one line added under `types`, keeping the
    file's comments; the decision is logged. Until then the type is not assessed."""
    if not reviewer.strip():
        raise ValueError("a class is approved by a named person")
    if not _TYPE.match(resource_type):
        raise ValueError(f"{resource_type!r} is not a resource type")
    found = load(path)
    if cls not in found.intents:
        raise ValueError(f"no class {cls!r}; known: {sorted(found.intents)}")
    if resource_type in found.types:
        raise ValueError(f"{resource_type} is already {found.types[resource_type]}")
    lines = path.read_text(encoding="utf-8").split("\n")
    at = lines.index("types:") + 1
    while at < len(lines) and lines[at].startswith("  "):
        at += 1
    lines.insert(at, f"  {resource_type}: {cls}")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    load.cache_clear()
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "resource_type": resource_type,
        "class": cls,
        "reviewer": reviewer,
    }
    with (path.parent / REVIEW_LOG).open("a", encoding="utf-8", newline="\n") as log:
        log.write(json.dumps(entry) + "\n")
    return entry
