"""Declarative checks (spec.md A-127): long-tail tier-1 rules as YAML, read by a small,
total interpreter.

A check reads one collection of the tenant's current entities, filters it, abstains when
there is nothing to judge, narrows it to the population it judges, and requires every
member of that population to satisfy each condition:

    read         from persons, grants, assets or evidence, where every condition holds
    abstain      when nothing was read: needs_review, with a fixed reason
    population   the members of what was read that the rule judges (all, by default)
    require      what every member of the population must satisfy
    fail, pass   the reasoning, with {failing}, {population} and {failing_list} only

A condition names a field (a dot path into the entity, such as attributes.mfa_enabled or
settings.private) and one test: equals, in, truthy, at_least, at_most or exists. There are
no loops beyond the one collection, no expressions and no code, so a check cannot fail to
finish and cannot do anything but decide its status. Everything is checked at load: the
source, each field's first segment against the entity's model, the test's shape, and the
placeholders in the reasoning. A check is versioned by its file and this interpreter
(A-101), and its subjects and impact rows are declared like any other test's.
"""

from __future__ import annotations

import string
from collections.abc import Sequence
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ccp.evaluators.context import EvaluationContext
from ccp.evaluators.deterministic.common import latest, subject_key
from ccp.evaluators.verdict import Verdict
from ccp.knowledge.graph import KNOWLEDGE_DIR
from ccp.normalize.entities import AccessGrant, Asset, Evidence, Person
from ccp.types import VerdictStatus

CHECKS_DIR = KNOWLEDGE_DIR / "checks"
Source = Literal["persons", "grants", "assets", "evidence"]
_MODELS: dict[str, type[BaseModel]] = {
    "persons": Person,
    "grants": AccessGrant,
    "assets": Asset,
    "evidence": Evidence,
}
_TESTS = ("equals", "in", "truthy", "at_least", "at_most", "exists")
_FAIL_FIELDS = {"failing", "population", "failing_list"}
_PASS_FIELDS = {"population"}


class Condition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    field: str
    equals: Any = None
    in_: list[Any] | None = Field(default=None, alias="in")
    truthy: bool | None = None
    at_least: float | None = None
    at_most: float | None = None
    exists: bool | None = None

    @model_validator(mode="after")
    def _one_test(self) -> Condition:
        given = [name for name in _TESTS if self._value(name) is not None]
        if len(given) != 1:
            raise ValueError(f"{self.field}: one test of {_TESTS}, not {given or 'none'}")
        return self

    def _value(self, name: str) -> Any:
        return self.in_ if name == "in" else getattr(self, name)

    def holds(self, entity: BaseModel) -> bool:
        value = _field(entity, self.field)
        if self.equals is not None:
            return bool(value == self.equals)
        if self.in_ is not None:
            return value in self.in_
        if self.truthy is not None:
            return bool(value) is self.truthy
        if self.exists is not None:
            return (value is not None) is self.exists
        if not isinstance(value, int | float) or isinstance(value, bool):
            return False  # a bound on a missing or non-numeric field never holds
        if self.at_least is not None:
            return bool(value >= self.at_least)
        return self.at_most is not None and bool(value <= self.at_most)


class Read(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Source = Field(alias="from")
    where: tuple[Condition, ...] = ()


class Abstain(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    when: Literal["empty"] = "empty"
    reason: str
    events: tuple[str, ...] = ()


class Population(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    where: tuple[Condition, ...] = ()


class Check(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    test_id: str
    subject: Literal["tenant"]
    read: Read
    abstain: Abstain
    population: Population = Population()
    require: tuple[Condition, ...] = Field(min_length=1)
    fail: str
    failing_list: str
    pass_: str = Field(alias="pass")
    path: Path | None = None  # where it was read from, for its rule version

    @model_validator(mode="after")
    def _fields_and_templates(self) -> Check:
        model = _MODELS[self.read.source]
        for condition in (*self.read.where, *self.population.where, *self.require):
            _known_field(model, condition.field)
        _known_field(model, self.failing_list)
        _placeholders(self.fail, _FAIL_FIELDS, "fail")
        _placeholders(self.pass_, _PASS_FIELDS, "pass")
        return self


def _known_field(model: type[BaseModel], path: str) -> None:
    root = path.split(".", 1)[0]
    if root not in model.model_fields:
        raise ValueError(f"{model.__name__} has no field {root!r}")


def _placeholders(template: str, allowed: set[str], name: str) -> None:
    used = {f for _, f, _, _ in string.Formatter().parse(template) if f is not None}
    if used - allowed:
        raise ValueError(
            f"{name}: only {sorted(allowed)} may be filled, not {sorted(used - allowed)}"
        )


def _field(entity: BaseModel, path: str) -> Any:
    """A dot path into an entity; a missing step is None, an enum its value."""
    value: Any = entity
    for step in path.split("."):
        if isinstance(value, dict):
            value = value.get(step)
        elif isinstance(value, BaseModel):
            value = getattr(value, step, None)
        else:
            return None
        if value is None:
            return None
    return value.value if isinstance(value, Enum) else value


def file_stem(test_id: str) -> str:
    """The file a test's check lives in: HR-2 -> hr2.yaml."""
    return test_id.lower().replace("-", "")


def load_all(directory: Path = CHECKS_DIR) -> dict[str, Check]:
    checks: dict[str, Check] = {}
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        check = Check.model_validate({**raw, "path": path})
        if path.stem != file_stem(check.test_id):
            raise ValueError(
                f"{path.name}: a check is named after its test, {file_stem(check.test_id)}"
            )
        if check.test_id in checks:
            raise ValueError(f"two declarative checks for {check.test_id}")
        checks[check.test_id] = check
    return checks


def _collection(ctx: EvaluationContext, source: str) -> Sequence[BaseModel]:
    if source == "persons":
        return [ctx.persons[k] for k in sorted(ctx.persons)]
    if source == "grants":
        return ctx.grants
    if source == "assets":
        return [ctx.assets[k] for k in sorted(ctx.assets)]
    return ctx.evidence


def subjects(ctx: EvaluationContext, check: Check) -> list[str]:
    return ["tenant"]


def evaluate(ctx: EvaluationContext, check: Check, subject: str) -> Verdict:
    key = subject_key(ctx, subject)
    read = [
        e for e in _collection(ctx, check.read.source) if all(c.holds(e) for c in check.read.where)
    ]
    observed: datetime | None = latest(*(getattr(e, "observed_at", None) for e in read))
    valid_from = ctx.valid_from(evidence_observed_at=observed or ctx.trigger_observed_at)
    refs = [str(getattr(e, "entity_id", "")) for e in read]
    if not read:
        return ctx.verdict(
            key,
            VerdictStatus.needs_review,
            check.abstain.reason,
            refs,
            valid_from,
            check.abstain.events,
        )
    population = [e for e in read if all(c.holds(e) for c in check.population.where)]
    failing = [e for e in population if not all(c.holds(e) for c in check.require)]
    if failing:
        listed = ", ".join(str(_field(e, check.failing_list)) for e in failing)
        reasoning = check.fail.format(
            failing=len(failing), population=len(population), failing_list=listed
        )
        return ctx.verdict(key, VerdictStatus.failed, reasoning, refs, valid_from)
    reasoning = check.pass_.format(population=len(population))
    return ctx.verdict(key, VerdictStatus.passed, reasoning, refs, valid_from)
