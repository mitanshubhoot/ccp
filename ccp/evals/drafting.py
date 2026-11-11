"""Declarative checks drafted by a model offline, gated by code, promoted by a person
(spec.md A-165). `ccp knowledge draft-check` and `ccp knowledge review-check`.

A model writes a draft of a test's declarative check (A-127) from the test's description,
the fields of the entity it reads, and recorded entities of the tenants given. The draft
is data, never code, and nothing it says reaches a verdict until four gates pass and a
named reviewer accepts it:

    compile   the draft loads as a declarative check: a known source, a root field of the
              entity on every condition, only the allowed placeholders
    paths     every field the draft reads resolves to a value on at least one recorded
              entity it would read: a path that is always empty is a typo, not a rule
    fixtures  the draft decides every tenant given, without raising
    shadow    on each tenant the draft's status matches today's evaluator for the test
              (the worst status over its subjects), or the labelled status given; any
              difference is listed and fails the gate

A passing, accepted draft is written into ccp/knowledge/checks/, which makes a new
catalogue version (A-128); verdicts at run time stay deterministic.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ccp.config import Settings
from ccp.evaluators import context as contexts
from ccp.evaluators import deterministic
from ccp.evaluators.deterministic import declarative
from ccp.evaluators.extraction import schema_block
from ccp.knowledge import catalogue
from ccp.knowledge import graph as knowledge
from ccp.knowledge.graph import KNOWLEDGE_DIR, TestSpec
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import LLMClient, SystemBlock, try_build_client
from ccp.llm.prompts import load_prompt
from ccp.security import prompt_guard
from ccp.storage import db as storage
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from ccp.types import VerdictStatus

PROMPT_VERSION = "check_draft_v1"
DRAFTS = "check_drafts"
SAMPLE = 5
_RANK = {VerdictStatus.passed: 0, VerdictStatus.needs_review: 1, VerdictStatus.failed: 2}


class DraftCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    test: Literal["equals", "in", "truthy", "at_least", "at_most", "exists"]
    value_json: str = Field(description='the test\'s value as JSON: true, 3, "active", [...]')


class DraftedCheck(BaseModel):
    """What the model writes: a declarative check's parts, as data."""

    model_config = ConfigDict(extra="forbid")

    read_from: Literal["persons", "grants", "assets", "evidence"]
    read_where: list[DraftCondition]
    abstain_reason: str = Field(max_length=200)
    population_where: list[DraftCondition]
    require: list[DraftCondition] = Field(min_length=1)
    fail: str = Field(max_length=300)
    pass_text: str = Field(max_length=300)
    failing_list: str


def _condition(c: DraftCondition) -> dict[str, Any]:
    try:
        value = json.loads(c.value_json)
    except ValueError:
        value = c.value_json
    return {"field": c.field, c.test: value}


def to_document(test_id: str, draft: DraftedCheck) -> dict[str, Any]:
    """The draft as the YAML document a check file holds."""
    doc: dict[str, Any] = {
        "test_id": test_id,
        "subject": "tenant",
        "read": {"from": draft.read_from, "where": [_condition(c) for c in draft.read_where]},
        "abstain": {"reason": draft.abstain_reason},
    }
    if draft.population_where:
        doc["population"] = {"where": [_condition(c) for c in draft.population_where]}
    doc["require"] = [_condition(c) for c in draft.require]
    doc["fail"] = draft.fail
    doc["failing_list"] = draft.failing_list
    doc["pass"] = draft.pass_text
    return doc


@dataclass
class Gates:
    compile: tuple[bool, str] = (False, "")
    paths: tuple[bool, str] = (False, "")
    fixtures: tuple[bool, str] = (False, "")
    shadow: tuple[bool, str] = (False, "")
    diffs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(g[0] for g in (self.compile, self.paths, self.fixtures, self.shadow))


def _collection(ctx: contexts.EvaluationContext, source: str) -> list[BaseModel]:
    if source == "persons":
        return list(ctx.persons.values())
    if source == "grants":
        return list(ctx.grants)
    if source == "assets":
        return list(ctx.assets.values())
    return list(ctx.evidence)


@dataclass(frozen=True)
class Outcome:
    """One tenant-level result: the status, and what fails (names when known, else how
    many subjects)."""

    status: VerdictStatus
    failing: int
    names: tuple[str, ...] | None


def _names(verdict: Any) -> tuple[str, ...]:
    return tuple(sorted(f["name"] for f in (verdict.extracted_fields or {}).get("failing", [])))


def _baseline(ctx: contexts.EvaluationContext) -> Outcome | None:
    """Today's evaluator for the test, as one tenant-level outcome: its worst subject."""
    test_id = ctx.spec.test_id
    if test_id not in deterministic.MODULES and test_id not in deterministic.DECLARATIVE:
        return None
    verdicts = [deterministic.evaluate(ctx, s) for s in deterministic.subjects_for(ctx)]
    if not verdicts:
        return None
    worst = max((v.status for v in verdicts), key=lambda st: _RANK.get(st, 1))
    if test_id in deterministic.DECLARATIVE:
        names = _names(verdicts[0])
        return Outcome(worst, len(names), names)
    failing = sum(1 for v in verdicts if v.status == VerdictStatus.failed)
    return Outcome(worst, failing, None)


def check_gates(
    doc: dict[str, Any],
    spec: TestSpec,
    tenants: list[ScopedDB],
    settings: Settings,
    labels: dict[str, str] | None = None,
    now: datetime | None = None,
) -> tuple[declarative.Check | None, Gates]:
    gates = Gates()
    try:
        check = declarative.Check.model_validate(doc)
    except ValidationError as exc:
        gates.compile = (False, str(exc.errors()[0].get("msg", exc))[:300])
        return None, gates
    gates.compile = (True, "")
    moment = now or datetime.now(UTC)
    fields = {c.field for c in (*check.read.where, *check.population.where, *check.require)} | {
        check.failing_list
    }
    resolved: set[str] = set()
    errors: list[str] = []
    for db in tenants:
        ctx = contexts.load_context(db, settings, spec, moment, moment)
        for entity in _collection(ctx, check.read.source):
            resolved |= {f for f in fields if declarative._field(entity, f) is not None}
        try:
            verdict = declarative.evaluate(ctx, check, "tenant")
        except Exception as exc:  # a draft that cannot decide is refused, whatever the cause
            errors.append(f"{db.tenant.tenant_id}: {type(exc).__name__}: {exc}")
            continue
        expected_label = (labels or {}).get(db.tenant.tenant_id)
        names = _names(verdict)
        mine = Outcome(verdict.status, len(names), names)
        today = (
            Outcome(VerdictStatus(expected_label), mine.failing, None)
            if expected_label
            else _baseline(ctx)
        )
        if today is not None and (
            mine.status != today.status
            or mine.failing != today.failing
            or (today.names is not None and mine.names != today.names)
        ):
            gates.diffs.append(
                {
                    "tenant": db.tenant.tenant_id,
                    "draft": mine.status.value,
                    "today": today.status.value,
                    "draft_failing": list(mine.names or ()),
                    "today_failing": list(today.names or ()) or today.failing,
                    "reasoning": verdict.reasoning,
                }
            )
    dead = sorted(fields - resolved)
    gates.paths = (not dead, ", ".join(dead))
    gates.fixtures = (not errors, "; ".join(errors))
    gates.shadow = (not gates.diffs, f"{len(gates.diffs)} difference(s)" if gates.diffs else "")
    return check, gates


def _samples(
    tenants: list[ScopedDB], settings: Settings, spec: TestSpec, source: str
) -> list[dict[str, Any]]:
    moment = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for db in tenants:
        ctx = contexts.load_context(db, settings, spec, moment, moment)
        for entity in _collection(ctx, source)[:SAMPLE]:
            out.append(entity.model_dump(mode="json", exclude={"text", "page_texts"}))
    return out


def draft(
    llm: LLMClient,
    settings: Settings,
    spec: TestSpec,
    tenants: list[ScopedDB],
    source: str,
) -> DraftedCheck:
    """One tool-less call: the test, the entity's fields and recorded samples in, a draft
    out. The samples are customer data, so they reach the model as a fenced document."""
    model = declarative._MODELS[source]
    nonce = prompt_guard.new_nonce()
    block = prompt_guard.frame_evidence(
        [
            prompt_guard.FramedChunk(
                chunk_id=f"samples:{source}",
                text=json.dumps(_samples(tenants, settings, spec, source), default=str)[:12000],
                evidence_type="recorded_entities",
            )
        ],
        nonce,
    )
    fields_json = json.dumps(model.model_json_schema()["properties"])[:6000]
    test = {
        "test_id": spec.test_id,
        "title": spec.title,
        "evidence_types": [e.value for e in spec.evidence_types],
    }
    result = llm.extract(
        schema=DraftedCheck,
        system=[
            SystemBlock(load_prompt(PROMPT_VERSION).text),
            SystemBlock(schema_block(DraftedCheck), cache=True),
        ],
        user=(
            f"{prompt_guard.fence_declaration(nonce)}\n\n"
            f"The test (system data): {json.dumps(test)}\n"
            f"Reads: {source}; the entity's fields: {fields_json}"
        ),
        model=settings.ccp_drafting_model,
        tracker=BudgetTracker(
            RunBudget.from_settings(settings).lowered(max_seconds=120, max_usd=1.0)
        ),
        max_tokens=2048,
        documents=[block.text],
    )
    drafted: DraftedCheck = result.parsed
    return drafted


def save(
    test_id: str, doc: dict[str, Any], gates: Gates, knowledge_dir: Path = KNOWLEDGE_DIR
) -> Path:
    folder = knowledge_dir / DRAFTS
    folder.mkdir(exist_ok=True)
    stem = declarative.file_stem(test_id)
    path = folder / f"{stem}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    record = {
        "test_id": test_id,
        "at": datetime.now(UTC).isoformat(),
        **asdict(gates),
        "passed": gates.passed,
    }
    (folder / f"{stem}.gates.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def review(
    test_id: str,
    reviewer: str,
    accept: bool,
    note: str = "",
    knowledge_dir: Path = KNOWLEDGE_DIR,
) -> dict[str, Any]:
    """A named reviewer's decision on a draft. An accept is refused unless every gate
    passed; it writes the check into checks/, a new catalogue version."""
    if not reviewer.strip():
        raise ValueError("a drafted check is reviewed by a named person")
    stem = declarative.file_stem(test_id)
    folder = knowledge_dir / DRAFTS
    gates = json.loads((folder / f"{stem}.gates.json").read_text(encoding="utf-8"))
    before = catalogue.version(knowledge_dir)
    promoted = False
    if accept:
        if not gates.get("passed"):
            failed = [k for k in ("compile", "paths", "fixtures", "shadow") if not gates[k][0]]
            raise ValueError(f"the draft failed {failed}; it cannot be accepted")
        target = knowledge_dir / "checks" / f"{stem}.yaml"
        header = (
            f"# {test_id} as a declarative check, drafted offline and reviewed by {reviewer}"
            " (A-165).\n"
        )
        target.write_text(
            header + (folder / f"{stem}.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        catalogue.version.cache_clear()
        promoted = True
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "test_id": test_id,
        "decision": "accept" if accept else "reject",
        "reviewer": reviewer,
        "note": note,
        "promoted": promoted,
        "catalogue_before": before,
        "catalogue_after": catalogue.version(knowledge_dir),
    }
    with (folder / "reviews.jsonl").open("a", encoding="utf-8", newline="\n") as log:
        log.write(json.dumps(entry) + "\n")
    return entry


def spec_of(test_id: str) -> TestSpec:
    found = knowledge.load().tests.get(test_id)
    if found is None:
        raise ValueError(f"no test {test_id!r}")
    return found


app = typer.Typer(no_args_is_help=True, help="Declarative checks drafted offline, then reviewed")


@app.command("draft")
def draft_cmd(
    test: str = typer.Option(..., "--test"),
    source: str = typer.Option(..., "--from", help="persons, grants, assets or evidence"),
    tenant: list[str] = typer.Option(..., "--tenant", help="tenants whose data gates it"),
) -> None:
    """Draft a check with the model, run the gates, and save both for review."""
    settings = Settings()
    llm = try_build_client(settings)
    if llm is None:
        raise typer.BadParameter("drafting needs ANTHROPIC_API_KEY")
    spec = spec_of(test)
    pool = storage.app_pool(settings)
    try:
        dbs = [ScopedDB(pool, TenantContext.for_id(settings, t)) for t in tenant]
        doc = to_document(test, draft(llm, settings, spec, dbs, source))
        _, gates = check_gates(doc, spec, dbs, settings)
        path = save(test, doc, gates)
        typer.echo(
            json.dumps({**asdict(gates), "passed": gates.passed, "draft": str(path)}, indent=2)
        )
    finally:
        pool.close()


@app.command("review")
def review_cmd(
    test: str = typer.Option(..., "--test"),
    by: str = typer.Option(..., "--by"),
    accept: bool = typer.Option(False, "--accept"),
    reject: bool = typer.Option(False, "--reject"),
    note: str = typer.Option("", "--note"),
) -> None:
    if accept == reject:
        raise typer.BadParameter("pass exactly one of --accept and --reject")
    try:
        typer.echo(json.dumps(review(test, by, accept, note), indent=2))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
