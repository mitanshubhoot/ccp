"""The knowledge graph: typed models, the YAML loader, the router, and the reference-data
tables (not tenant data; the one place a plain upsert is allowed).

    Framework ──< Requirement >──< Control >──< TestSpec
                   (soc2:CC6.2)    (CTL-ACCESS-REVOKE)   (HR-1, GH-1)

Reuse is structural: one control maps to requirements in both frameworks, so one verdict
row fans out into both framework rollups.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ccp.storage.scoped import ScopedDB
from ccp.types import EvidenceType

KNOWLEDGE_DIR = Path(__file__).resolve().parent
Evaluation = Literal["deterministic", "interpretive"]
SubjectKind = Literal["tenant", "person", "repo", "org"]
ValidFromStrategy = Literal[
    "evidence_observed", "event_time", "event_time_plus_sla", "trigger_time"
]
Tier = Literal[1, 2, 3]


class Requirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: str
    title: str
    intent: str
    clauses: tuple[str, ...]


class Framework(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    framework_id: str
    name: str
    requirements: tuple[Requirement, ...]

    def requirement(self, requirement_id: str) -> Requirement:
        for r in self.requirements:
            if r.requirement_id == requirement_id:
                return r
        raise KeyError(f"{self.framework_id} has no requirement {requirement_id!r}")


class Control(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    control_id: str
    title: str
    description: str
    requirements: tuple[str, ...]  # "framework_id:requirement_id"
    tests: tuple[str, ...]

    def requirements_in(self, framework_id: str) -> list[str]:
        return [r.split(":", 1)[1] for r in self.requirements if r.startswith(framework_id + ":")]


class TestSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    test_id: str
    title: str
    connector_scope: tuple[str, ...]
    evaluation: Evaluation
    evidence_types: tuple[EvidenceType, ...] = Field(min_length=1)
    subject_kind: SubjectKind
    controls: tuple[str, ...] = Field(min_length=1)
    rule: str
    valid_from_strategy: ValidFromStrategy
    parameters: dict[str, Any] = Field(default_factory=dict)
    prompt_version: str | None = None
    extraction_schema: str | None = None
    evidence_max_age_days: int | None = None
    revalidate_every_days: int | None = None

    @model_validator(mode="after")
    def _interpretive_needs_extraction(self) -> TestSpec:
        if self.evaluation == "interpretive" and not (
            self.prompt_version and self.extraction_schema
        ):
            raise ValueError(
                f"{self.test_id}: interpretive tests declare prompt_version and extraction_schema"
            )
        if self.evaluation == "deterministic" and (self.prompt_version or self.extraction_schema):
            raise ValueError(f"{self.test_id}: deterministic tests never call a model")
        return self

    @property
    def tier(self) -> Tier:
        return tier_for(self)

    @property
    def evidence_max_age(self) -> timedelta | None:
        return timedelta(days=self.evidence_max_age_days) if self.evidence_max_age_days else None

    @property
    def revalidate_every(self) -> timedelta | None:
        return timedelta(days=self.revalidate_every_days) if self.revalidate_every_days else None


def tier_for(spec: TestSpec) -> Tier:
    """The router (spec.md Layer 6): deterministic -> 1; one evidence type -> 2; more -> 3."""
    if spec.evaluation == "deterministic":
        return 1
    return 2 if len(spec.evidence_types) == 1 else 3


class KnowledgeGraph(BaseModel):
    model_config = ConfigDict(frozen=True)

    frameworks: dict[str, Framework]
    controls: dict[str, Control]
    tests: dict[str, TestSpec]

    @model_validator(mode="after")
    def _edges_resolve(self) -> KnowledgeGraph:
        for control in self.controls.values():
            for ref in control.requirements:
                fw, _, req = ref.partition(":")
                if fw not in self.frameworks:
                    raise ValueError(f"{control.control_id}: unknown framework {fw!r}")
                try:
                    self.frameworks[fw].requirement(req)
                except KeyError as exc:
                    raise ValueError(f"{control.control_id}: {exc.args[0]}") from None
            for test_id in control.tests:
                if test_id not in self.tests:
                    raise ValueError(f"{control.control_id}: unknown test {test_id!r}")
        for spec in self.tests.values():
            for control_id in spec.controls:
                if control_id not in self.controls:
                    raise ValueError(f"{spec.test_id}: unknown control {control_id!r}")
                if spec.test_id not in self.controls[control_id].tests:
                    raise ValueError(f"{spec.test_id}: {control_id} does not list it")
        return self

    def controls_for_test(self, test_id: str) -> list[Control]:
        return [self.controls[c] for c in self.tests[test_id].controls]

    def tests_for_requirement(self, framework_id: str, requirement_id: str) -> list[TestSpec]:
        out: list[TestSpec] = []
        for control in self.controls.values():
            if requirement_id in control.requirements_in(framework_id):
                out.extend(self.tests[t] for t in control.tests)
        return sorted({t.test_id: t for t in out}.values(), key=lambda t: t.test_id)

    def controls_for_requirement(self, framework_id: str, requirement_id: str) -> list[Control]:
        return [
            c for c in self.controls.values() if requirement_id in c.requirements_in(framework_id)
        ]


def load(directory: Path = KNOWLEDGE_DIR) -> KnowledgeGraph:
    frameworks: dict[str, Framework] = {}
    for path in sorted((directory / "frameworks").glob("*.yaml")):
        fw = Framework.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        frameworks[fw.framework_id] = fw
    controls_raw = yaml.safe_load((directory / "controls.yaml").read_text(encoding="utf-8"))
    controls = {c["control_id"]: Control.model_validate(c) for c in controls_raw["controls"]}
    tests_raw = yaml.safe_load((directory / "tests.yaml").read_text(encoding="utf-8"))
    tests = {t["test_id"]: TestSpec.model_validate(t) for t in tests_raw["tests"]}
    return KnowledgeGraph(frameworks=frameworks, controls=controls, tests=tests)


def load_into_db(db: ScopedDB, graph: KnowledgeGraph) -> dict[str, int]:
    """Upsert the graph into the reference tables. Reference data, not tenant data."""
    counts = {"frameworks": 0, "requirements": 0, "controls": 0, "tests": 0, "edges": 0}
    with db.transaction() as cur:
        for fw in graph.frameworks.values():
            cur.execute(
                "INSERT INTO frameworks (framework_id, name) VALUES (%s, %s)"
                " ON CONFLICT (framework_id) DO UPDATE SET name = EXCLUDED.name",
                (fw.framework_id, fw.name),
            )
            counts["frameworks"] += 1
            for req in fw.requirements:
                cur.execute(
                    "INSERT INTO requirements"
                    " (framework_id, requirement_id, title, intent, clauses)"
                    " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (framework_id, requirement_id)"
                    " DO UPDATE SET title = EXCLUDED.title, intent = EXCLUDED.intent,"
                    " clauses = EXCLUDED.clauses",
                    (fw.framework_id, req.requirement_id, req.title, req.intent, list(req.clauses)),
                )
                counts["requirements"] += 1
        for control in graph.controls.values():
            cur.execute(
                "INSERT INTO controls (control_id, title, description) VALUES (%s, %s, %s)"
                " ON CONFLICT (control_id) DO UPDATE SET title = EXCLUDED.title,"
                " description = EXCLUDED.description",
                (control.control_id, control.title, control.description),
            )
            counts["controls"] += 1
            for ref in control.requirements:
                fw_id, _, req_id = ref.partition(":")
                cur.execute(
                    "INSERT INTO requirement_controls (framework_id, requirement_id, control_id)"
                    " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (fw_id, req_id, control.control_id),
                )
                counts["edges"] += 1
        for spec in graph.tests.values():
            cur.execute(
                "INSERT INTO tests (test_id, title, evaluation, evidence_types, subject_kind, rule,"
                " valid_from_strategy, tier, spec) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (test_id) DO UPDATE SET title = EXCLUDED.title,"
                " evaluation = EXCLUDED.evaluation, evidence_types = EXCLUDED.evidence_types,"
                " subject_kind = EXCLUDED.subject_kind, rule = EXCLUDED.rule,"
                " valid_from_strategy = EXCLUDED.valid_from_strategy, tier = EXCLUDED.tier,"
                " spec = EXCLUDED.spec",
                (
                    spec.test_id,
                    spec.title,
                    spec.evaluation,
                    [e.value for e in spec.evidence_types],
                    spec.subject_kind,
                    spec.rule,
                    spec.valid_from_strategy,
                    spec.tier,
                    spec.model_dump_json(),
                ),
            )
            counts["tests"] += 1
            for control_id in spec.controls:
                cur.execute(
                    "INSERT INTO control_tests (control_id, test_id) VALUES (%s, %s)"
                    " ON CONFLICT DO NOTHING",
                    (control_id, spec.test_id),
                )
                counts["edges"] += 1
    return counts
