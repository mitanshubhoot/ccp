"""Code planners (spec.md A-150, report 6.8 "Planner, worker and judge"): scoping facts and
the control catalogue become one row per item, and only code decides the split.

    audit_prep      scope: a framework, optional control families, the audit period's end.
                    One check item per in-scope test, settled by code from the rules'
                    verdicts as the rollup reads them; once a family's checks settle with a
                    gap, code adds one read item for that family (its gap list), which a
                    model run fills
    questionnaire   scope: questions, each optionally mapped to a control. A mapped question
                    is a check item answered from the control's verdicts; a question code
                    cannot answer (unmapped, or its control undecided) becomes a read item.
                    The full questionnaire pipeline (library, prose, staleness) is LT3

A planner never runs a model, and a model never adds an item: a read run may name what is
missing, and code decides what that means.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ccp.evaluators import inventory
from ccp.knowledge import graph as knowledge
from ccp.knowledge import rollup as knowledge_rollup
from ccp.knowledge.graph import KnowledgeGraph
from ccp.storage import bitemporal
from ccp.storage.bitemporal import StateRow
from ccp.storage.scoped import ScopedDB
from ccp.tasks.store import Item, NewItem, Task

MAX_GAPS_PER_READ = 20  # a family's read item names at most this many gaps; the rest are counted


@dataclass(frozen=True)
class Settlement:
    """What code concluded about a check item this pass."""

    status: str  # final | open
    outcome: str
    result: dict[str, Any]
    evidence_ids: tuple[str, ...] = ()
    why: str = "checked"


@dataclass
class Facts:
    """What one pass reads once, for every check item: the heads and the rollup's coverage."""

    graph: KnowledgeGraph
    by_test: dict[str, list[StateRow]]
    coverage: knowledge_rollup.Coverage

    @classmethod
    def read(cls, db: ScopedDB, now: datetime | None = None) -> Facts:
        by_test: dict[str, list[StateRow]] = {}
        for row in bitemporal.heads(db):
            by_test.setdefault(row.test_id, []).append(row)
        return cls(knowledge.load(), by_test, inventory.coverage(db, now))

    def states(self, test_id: str) -> list[knowledge_rollup.Seen]:
        spec = self.graph.tests.get(test_id)
        if spec is None:
            return [knowledge_rollup.Seen("pending", None, why="unknown_test")]
        return knowledge_rollup.test_states(spec, self.by_test.get(test_id, []), self.coverage)


class Planner(Protocol):
    def plan(self, graph: KnowledgeGraph, scope: dict[str, Any]) -> list[NewItem]: ...

    def expected_reads(self, items: list[NewItem]) -> int: ...

    def settle(self, facts: Facts, item: Item) -> Settlement: ...

    def followups(self, task: Task, items: list[Item]) -> list[NewItem]: ...


def family_of(requirement_id: str) -> str:
    """A requirement's control family: CC6.1 -> CC6, A.5.15 -> A.5."""
    return requirement_id.rsplit(".", 1)[0] if "." in requirement_id else requirement_id


def _counts(states: list[knowledge_rollup.Seen]) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in states:
        out[s.state] = out.get(s.state, 0) + 1
    return dict(sorted(out.items()))


@dataclass
class AuditPrep:
    """One check per in-scope test; one gap read per control family with a gap."""

    kind: str = "audit_prep"

    def plan(self, graph: KnowledgeGraph, scope: dict[str, Any]) -> list[NewItem]:
        framework_id = str(scope["framework_id"])
        if framework_id not in graph.frameworks:
            raise ValueError(f"unknown framework {framework_id!r}")
        wanted = {str(f) for f in scope.get("families") or []}
        tests: dict[str, dict[str, set[str]]] = {}
        for req in graph.frameworks[framework_id].requirements:
            family = family_of(req.requirement_id)
            if wanted and family not in wanted:
                continue
            for spec in graph.tests_for_requirement(framework_id, req.requirement_id):
                seen = tests.setdefault(spec.test_id, {"requirements": set(), "families": set()})
                seen["requirements"].add(req.requirement_id)
                seen["families"].add(family)
        if not tests:
            raise ValueError(f"no tests in scope for {framework_id} {sorted(wanted) or ''}")
        return [
            NewItem(
                item_id=f"test:{test_id}",
                kind="check",
                question=(
                    f"Does {test_id} ({graph.tests[test_id].title}) hold for every subject"
                    f" in the period ending {scope.get('period_end') or 'now'}?"
                ),
                inputs={
                    "test_id": test_id,
                    "framework_id": framework_id,
                    "requirements": sorted(found["requirements"]),
                    "families": sorted(found["families"]),
                    "as_of": scope.get("period_end"),
                },
            )
            for test_id, found in sorted(tests.items())
        ]

    def expected_reads(self, items: list[NewItem]) -> int:
        return len({f for i in items for f in i.inputs.get("families", [])})

    def settle(self, facts: Facts, item: Item) -> Settlement:
        test_id = str(item.inputs["test_id"])
        states = facts.states(test_id)
        counts = _counts(states)
        gaps = [
            {"subject_key": s.subject_key, "state": s.state, "verdict_id": s.verdict_id}
            for s in states
            if s.state in ("fail", "needs_review", "partial")
        ]
        pending = [
            {"subject_key": s.subject_key, "why": s.why} for s in states if s.state == "pending"
        ]
        evidence = tuple(sorted({str(g["verdict_id"]) for g in gaps if g["verdict_id"]}))
        result = {"test_id": test_id, "counts": counts, "gaps": gaps, "pending": pending}
        if gaps:
            return Settlement("final", "gap", result, evidence, "verdicts show a gap")
        if pending:  # not known yet: checked again on the next pass, never a pass
            return Settlement("open", "pending", result, (), "subjects not yet assessed")
        verdicts = tuple(sorted({str(s.verdict_id) for s in states if s.verdict_id}))
        return Settlement("final", "pass", result, verdicts, "every subject passes")

    def followups(self, task: Task, items: list[Item]) -> list[NewItem]:
        by_family: dict[str, list[Item]] = {}
        for it in items:
            if it.kind != "check":
                continue
            for family in it.inputs.get("families", []):
                by_family.setdefault(str(family), []).append(it)
        out: list[NewItem] = []
        framework_id = str(task.scope["framework_id"])
        for family, checks in sorted(by_family.items()):
            if any(c.status != "final" for c in checks):
                continue  # the family's list is written once all its checks are known
            gaps = [
                {"test_id": c.inputs["test_id"], **g}
                for c in checks
                if c.outcome == "gap"
                for g in (c.result or {}).get("gaps", [])
            ]
            if not gaps:
                continue
            shown = gaps[:MAX_GAPS_PER_READ]
            out.append(
                NewItem(
                    item_id=f"family:{family}",
                    kind="read",
                    question=(
                        f"Control family {family} of {framework_id} has {len(gaps)} failing or"
                        " unresolved results. For each listed gap, say what the evidence shows"
                        " is missing or wrong for the audit period, citing the evidence."
                    ),
                    inputs={
                        "framework_id": framework_id,
                        "family": family,
                        "gaps": shown,
                        "gaps_not_shown": len(gaps) - len(shown),
                        "tests": sorted({str(g["test_id"]) for g in gaps}),
                        "verdict_ids": sorted(
                            {str(g["verdict_id"]) for g in shown if g.get("verdict_id")}
                        ),
                        "as_of": task.scope.get("period_end"),
                    },
                )
            )
        return out


@dataclass
class Questionnaire:
    """A mapped question is answered from its control's verdicts; the rest are read."""

    kind: str = "questionnaire"

    def plan(self, graph: KnowledgeGraph, scope: dict[str, Any]) -> list[NewItem]:
        questions = list(scope.get("questions") or [])
        if not questions:
            raise ValueError("a questionnaire task needs questions")
        out: list[NewItem] = []
        for q in questions:
            qid = str(q["id"])
            text = str(q["text"]).strip()
            control_id = q.get("control_id")
            inputs: dict[str, Any] = {"question_id": qid, "text": text}
            if q.get("evidence_type"):
                inputs["evidence_type"] = str(q["evidence_type"])
            if control_id and control_id in graph.controls:
                inputs["control_id"] = str(control_id)
                inputs["tests"] = list(graph.controls[str(control_id)].tests)
                out.append(NewItem(f"q:{qid}", "check", text, inputs))
            else:
                if control_id:
                    inputs["unknown_control"] = str(control_id)
                out.append(NewItem(f"q:{qid}:read", "read", text, inputs))
        return out

    def expected_reads(self, items: list[NewItem]) -> int:
        return len(items)  # a mapped question may still need a read

    def settle(self, facts: Facts, item: Item) -> Settlement:
        tests = [str(t) for t in item.inputs.get("tests", [])]
        states = [s for t in tests for s in facts.states(t)]
        counts = _counts(states)
        verdicts = tuple(sorted({str(s.verdict_id) for s in states if s.verdict_id}))
        result = {"control_id": item.inputs.get("control_id"), "tests": tests, "counts": counts}
        if any(s.state == "fail" for s in states):
            return Settlement(
                "final", "no", {**result, "answer": "no"}, verdicts, "the control fails"
            )
        if any(s.state == "pending" for s in states):
            return Settlement("open", "pending", result, (), "the control is not yet assessed")
        if states and all(s.state == "pass" for s in states):
            return Settlement(
                "final", "yes", {**result, "answer": "yes"}, verdicts, "the control passes"
            )
        return Settlement(
            "final", "undecided", result, verdicts, "the verdicts do not decide the question"
        )

    def followups(self, task: Task, items: list[Item]) -> list[NewItem]:
        return [
            NewItem(
                f"{it.item_id}:read",
                "read",
                it.question,
                {**it.inputs, "verdict_ids": list(it.evidence_ids)},
            )
            for it in items
            if it.kind == "check" and it.status == "final" and it.outcome == "undecided"
        ]


PLANNERS: dict[str, Planner] = {"audit_prep": AuditPrep(), "questionnaire": Questionnaire()}


def planner_for(kind: str) -> Planner:
    try:
        return PLANNERS[kind]
    except KeyError:
        raise ValueError(f"unknown task kind {kind!r}; known: {sorted(PLANNERS)}") from None
