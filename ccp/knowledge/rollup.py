"""Requirement state is a rule, not a summary (spec.md D16).

    requirement state = worst state among its linked tests' current rows,
    precedence: fail > needs_review > pending > partial > pass,
    pending = a linked test with no row at all (parked or not yet evaluated),
    and the rollup names the blocking (test_id, subject_key).

A tenant with no rows at all yields an explicit no_evaluations_yet state, never an empty
list, so an empty report can never read as a clean one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ccp.knowledge.graph import KnowledgeGraph
from ccp.storage.bitemporal import StateRow
from ccp.types import VerdictStatus

RequirementState = Literal["fail", "needs_review", "pending", "partial", "pass"]
PRECEDENCE: tuple[RequirementState, ...] = ("fail", "needs_review", "pending", "partial", "pass")


@dataclass(frozen=True)
class RequirementRollup:
    framework_id: str
    requirement_id: str
    state: RequirementState
    blocking_test_id: str | None
    blocking_subject_key: str | None
    verdict_ids: tuple[str, ...]
    counts: dict[str, int] = field(default_factory=dict)
    tests: tuple[str, ...] = ()


@dataclass(frozen=True)
class FrameworkRollup:
    framework_id: str
    no_evaluations_yet: bool
    requirements: tuple[RequirementRollup, ...]

    @property
    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {s: 0 for s in PRECEDENCE}
        for r in self.requirements:
            out[r.state] += 1
        return out


_STATE_OF: dict[VerdictStatus, RequirementState] = {
    VerdictStatus.failed: "fail",
    VerdictStatus.needs_review: "needs_review",
    VerdictStatus.partial: "partial",
    VerdictStatus.passed: "pass",
}


def _state_of(row: StateRow) -> RequirementState:
    return _STATE_OF[row.status]


def rollup(graph: KnowledgeGraph, heads: list[StateRow], framework_id: str) -> FrameworkRollup:
    framework = graph.frameworks[framework_id]
    by_test: dict[str, list[StateRow]] = {}
    for row in heads:
        by_test.setdefault(row.test_id, []).append(row)
    requirements: list[RequirementRollup] = []
    for req in framework.requirements:
        specs = graph.tests_for_requirement(framework_id, req.requirement_id)
        worst: RequirementState = "pass"
        blocking: tuple[str, str | None] | None = None
        verdict_ids: list[str] = []
        counts: dict[str, int] = {s: 0 for s in PRECEDENCE}
        for spec in specs:
            rows = by_test.get(spec.test_id, [])
            if not rows:
                counts["pending"] += 1
                if PRECEDENCE.index("pending") < PRECEDENCE.index(worst):
                    worst, blocking = "pending", (spec.test_id, None)
                continue
            for row in rows:
                state = _state_of(row)
                counts[state] += 1
                verdict_ids.append(row.verdict_id)
                if PRECEDENCE.index(state) < PRECEDENCE.index(worst):
                    worst, blocking = state, (row.test_id, row.subject_key)
        if not specs:
            worst, blocking = "pending", None
        requirements.append(
            RequirementRollup(
                framework_id=framework_id,
                requirement_id=req.requirement_id,
                state=worst,
                blocking_test_id=blocking[0] if blocking else None,
                blocking_subject_key=blocking[1] if blocking else None,
                verdict_ids=tuple(sorted(set(verdict_ids))),
                counts=counts,
                tests=tuple(s.test_id for s in specs),
            )
        )
    return FrameworkRollup(
        framework_id=framework_id,
        no_evaluations_yet=not heads,
        requirements=tuple(requirements),
    )


def format_rollup(result: FrameworkRollup) -> str:
    lines = [f"framework {result.framework_id}"]
    if result.no_evaluations_yet:
        lines.append("  NO EVALUATIONS YET: nothing has been evaluated for this tenant")
    for r in result.requirements:
        blocker = ""
        if r.blocking_test_id:
            blocker = f"  blocked by {r.blocking_test_id}"
            if r.blocking_subject_key:
                blocker += f" on {r.blocking_subject_key}"
        lines.append(f"  {r.requirement_id:<8} {r.state:<13} tests={','.join(r.tests)}{blocker}")
    summary = ", ".join(f"{k}={v}" for k, v in result.summary.items())
    lines.append(f"  summary: {summary}")
    return "\n".join(lines)
