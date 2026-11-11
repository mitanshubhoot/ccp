"""Gold sets sized to the false-PASS bound, weighted towards FAIL (spec.md A-144, 6.6).

A false PASS is the costly error, so a gold set must hold enough FAIL-labelled cases to
bound it. With zero false passes observed among n FAIL cases, the one-sided upper bound on
the false-PASS rate at confidence c is 1 - (1 - c)^(1/n); the n needed for a bound b is
ceil(ln(1 - c) / ln(1 - b)). For b = 2% at 95% that is 149 FAIL cases. A target whose
cases are mostly PASS cannot show a low false-PASS rate however many it has, so each AI
target is also checked for a FAIL share of at least half.

`ccp evals size` reports, per model-backed target in the golden set, the FAIL-labelled cases
it has and needs, and the share; labelling to close the gap is people's work (MT7).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ccp.evals.schema import GoldenCase

AI_TARGETS = frozenset({"tier2_verdict", "tier3_verdict", "extraction", "investigation"})


def fail_cases_needed(bound: float, confidence: float = 0.95) -> int:
    if not 0 < bound < 1 or not 0 < confidence < 1:
        raise ValueError("bound and confidence lie strictly between 0 and 1")
    return math.ceil(math.log(1 - confidence) / math.log(1 - bound))


def upper_bound(fail_cases: int, confidence: float = 0.95) -> float:
    """The false-PASS upper bound when none of `fail_cases` passed."""
    return 1.0 if fail_cases <= 0 else 1 - (1 - confidence) ** (1 / fail_cases)


@dataclass(frozen=True)
class TargetSize:
    target: str
    cases: int
    fail_cases: int
    needed: int
    min_share: float

    @property
    def share(self) -> float:
        return self.fail_cases / self.cases if self.cases else 0.0

    @property
    def findings(self) -> list[str]:
        out: list[str] = []
        if self.fail_cases < self.needed:
            out.append(f"{self.fail_cases} FAIL cases, {self.needed} needed")
        if self.share < self.min_share:
            out.append(f"FAIL share {self.share:.0%} < {self.min_share:.0%}")
        return out


def _labelled_fail(case: GoldenCase) -> bool:
    status = case.expected.get("status")
    return isinstance(status, str) and status.lower() == "fail"


def size(
    cases: list[GoldenCase], bound: float, confidence: float = 0.95, min_share: float = 0.5
) -> list[TargetSize]:
    needed = fail_cases_needed(bound, confidence)
    by_target: dict[str, list[GoldenCase]] = {}
    for case in cases:
        if case.target in AI_TARGETS:
            by_target.setdefault(case.target, []).append(case)
    return [
        TargetSize(
            target,
            len(found),
            sum(1 for c in found if _labelled_fail(c)),
            needed,
            min_share,
        )
        for target, found in sorted(by_target.items())
    ]
