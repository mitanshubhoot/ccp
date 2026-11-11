"""Requirement state is a rule, not a summary (spec.md D16, A-124).

    requirement state = worst state among its linked tests' subjects,
    precedence: fail > needs_review > pending > partial > pass,
    and the rollup names the blocking (test_id, subject_key).

What counts as a subject's state depends on what the rollup is given. With the state heads
alone, a linked test with no row at all is pending. With coverage (A-124), each test's
population is the subjects its newest full run expected plus any it has a verdict for, and
a subject is pending, never passing, when:

    unassessed        it was expected and has no verdict
    parked            its latest evaluation is parked (no model key, or the budget spent)
    stale             its PASS or PARTIAL is a day past its revalidation interval
    and for the whole test, when its inventory is missing, was listed from a source not
    seen whole (inventory_incomplete), or is older than its freshness (inventory_stale).

A FAIL or needs_review is kept whatever else holds: the last thing known was a problem. A
tenant with no rows at all yields an explicit no_evaluations_yet state, never an empty list,
so an empty report can never read as a clean one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from ccp.knowledge.graph import KnowledgeGraph, TestSpec
from ccp.storage.bitemporal import StateRow
from ccp.types import VerdictStatus

RequirementState = Literal["fail", "needs_review", "pending", "partial", "pass"]
PRECEDENCE: tuple[RequirementState, ...] = ("fail", "needs_review", "pending", "partial", "pass")
# The clock re-checks a PASS at its test's interval; one a day past that is no longer known.
STALE_GRACE = timedelta(days=1)


@dataclass(frozen=True)
class Inventory:
    """A test's expected subjects, as its newest full run listed them (A-124)."""

    subjects: frozenset[str]
    complete: bool
    computed_at: datetime
    fresh_until: datetime
    gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class Coverage:
    """What the rollup needs besides the heads to tell a pass from an unknown (A-124)."""

    inventories: Mapping[str, Inventory]
    parked: frozenset[str]
    now: datetime


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
    pending_reasons: dict[str, int] = field(default_factory=dict)  # why pending (A-124)


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


@dataclass(frozen=True)
class _Seen:
    """One thing the rollup counts for a test: a subject's state, or the test's own gap."""

    state: RequirementState
    subject_key: str | None
    verdict_id: str | None = None
    why: str | None = None  # the pending reason


def _seen(spec: TestSpec, rows: list[StateRow], coverage: Coverage | None) -> list[_Seen]:
    if coverage is None:  # the heads alone
        if not rows:
            return [_Seen("pending", None, why="no_verdicts")]
        return [_Seen(_state_of(r), r.subject_key, r.verdict_id) for r in rows]
    heads = {r.subject_key: r for r in rows}
    inventory = coverage.inventories.get(spec.test_id)
    out: list[_Seen] = []
    if inventory is None:
        out.append(_Seen("pending", None, why="no_inventory"))
    elif not inventory.complete:
        out.append(_Seen("pending", None, why="inventory_incomplete"))
    elif coverage.now > inventory.fresh_until:
        out.append(_Seen("pending", None, why="inventory_stale"))
    expected = inventory.subjects if inventory is not None else frozenset()
    for subject in sorted(set(expected) | set(heads)):
        row = heads.get(subject)
        parked = subject in coverage.parked
        if row is None:
            out.append(_Seen("pending", subject, why="parked" if parked else "unassessed"))
            continue
        state = _state_of(row)
        if state in ("pass", "partial"):
            window = spec.revalidate_every
            if parked:
                out.append(_Seen("pending", subject, row.verdict_id, "parked"))
                continue
            if window is not None and row.recorded_at < coverage.now - window - STALE_GRACE:
                out.append(_Seen("pending", subject, row.verdict_id, "stale"))
                continue
        out.append(_Seen(state, subject, row.verdict_id))
    return out


def rollup(
    graph: KnowledgeGraph,
    heads: list[StateRow],
    framework_id: str,
    coverage: Coverage | None = None,
) -> FrameworkRollup:
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
        reasons: dict[str, int] = {}
        for spec in specs:
            for seen in _seen(spec, by_test.get(spec.test_id, []), coverage):
                counts[seen.state] += 1
                if seen.why:
                    reasons[seen.why] = reasons.get(seen.why, 0) + 1
                if seen.verdict_id:
                    verdict_ids.append(seen.verdict_id)
                if PRECEDENCE.index(seen.state) < PRECEDENCE.index(worst):
                    worst, blocking = seen.state, (spec.test_id, seen.subject_key)
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
                pending_reasons=reasons,
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
        why = ""
        if r.state == "pending" and r.pending_reasons:
            why = "  (" + ", ".join(f"{n} {k}" for k, n in sorted(r.pending_reasons.items())) + ")"
        lines.append(
            f"  {r.requirement_id:<8} {r.state:<13} tests={','.join(r.tests)}{blocker}{why}"
        )
    summary = ", ".join(f"{k}={v}" for k, v in result.summary.items())
    lines.append(f"  summary: {summary}")
    return "\n".join(lines)
