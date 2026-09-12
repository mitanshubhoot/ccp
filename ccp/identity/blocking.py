"""Blocking: cheap keys that decide which pairs are even compared.

Candidate pairs are generated only within a tenant, only across different source
connectors, and only where at least one block key matches. Each pair records which keys
produced it, so the decision log can say why two records were ever compared.

Keys:
  email:<normalized email>       lowercased; dots and a +suffix stripped from the local part
  local:<normalized local part>  the local part alone
  employee:<employee id>
  name:<first initial><surname>  ASCII-folded
  handle:<any handle value>      GitHub login, HRIS account name, ...
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from ccp.normalize.entities import Person


def normalize_email(email: str) -> str:
    local, _, domain = email.strip().lower().partition("@")
    local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@{domain}" if domain else local


def local_part(email: str) -> str:
    return normalize_email(email).partition("@")[0]


def fold(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)
    )


def name_key(display_name: str | None) -> str | None:
    if not display_name:
        return None
    tokens = [t.strip(".,") for t in fold(display_name).replace("-", " ").split() if t.strip(".,")]
    if len(tokens) < 2:
        return None
    return f"{tokens[0][0]}{tokens[-1]}"


def block_keys(person: Person) -> set[str]:
    keys: set[str] = set()
    for email in person.emails:
        keys.add(f"email:{normalize_email(email)}")
        keys.add(f"local:{local_part(email)}")
    if person.employee_id:
        keys.add(f"employee:{person.employee_id}")
    nk = name_key(person.display_name)
    if nk:
        keys.add(f"name:{nk}")
    for value in person.handles.values():
        keys.add(f"handle:{value.lower()}")
        keys.add(f"local:{value.lower().replace('.', '')}")
    return keys


@dataclass(frozen=True)
class CandidatePair:
    left: Person
    right: Person
    keys: tuple[str, ...]


def candidate_pairs(persons: Iterable[Person]) -> list[CandidatePair]:
    """Pairs of persons from different connectors sharing at least one block key."""
    people = list(persons)
    by_key: dict[str, list[Person]] = defaultdict(list)
    for person in people:
        for key in block_keys(person):
            by_key[key].append(person)
    pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
    for key, members in by_key.items():
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if a.tenant_id != b.tenant_id or a.source_connector == b.source_connector:
                    continue
                left, right = sorted((a, b), key=lambda p: p.entity_id)
                pairs[(left.entity_id, right.entity_id)].add(key)
    index = {p.entity_id: p for p in people}
    return [
        CandidatePair(index[left], index[right], tuple(sorted(keys)))
        for (left, right), keys in sorted(pairs.items())
    ]
