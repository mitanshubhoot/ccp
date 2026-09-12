"""Helpers shared by the deterministic rules."""

from __future__ import annotations

from datetime import datetime

from ccp.evaluators.context import EvaluationContext
from ccp.normalize.entities import Person
from ccp.types import EmploymentStatus


def subject_key(ctx: EvaluationContext, scope: str) -> str:
    return f"{ctx.spec.test_id}|{scope}"


def terminated_hr_persons(ctx: EvaluationContext) -> list[Person]:
    return sorted(
        (
            p
            for p in ctx.persons.values()
            if p.source_connector == "hris"
            and p.entity_id.startswith("hris:person:")
            and p.employment_status is EmploymentStatus.terminated
        ),
        key=lambda p: p.entity_id,
    )


def hr_person_for(ctx: EvaluationContext, canonical_id: str) -> Person | None:
    for entity_id in ctx.group(canonical_id):
        person = ctx.persons.get(entity_id)
        if person is not None and person.entity_id.startswith("hris:person:"):
            return person
    return None


def latest(*stamps: datetime | None) -> datetime | None:
    known = [s for s in stamps if s is not None]
    return max(known) if known else None
