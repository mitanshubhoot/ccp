"""Computed confidence (spec.md Layer 7 "Confidence cap, both AI tiers"). Never reported by
the model: the product of three measured factors, then the forcing rules.

    factor                 definition
    field coverage         required fields extracted with a verified citation (or honestly absent)
    citation rate          citations that survived verification
    retrieval quality      top score's percentile against the distractor noise floor

    forcing rule                                         effect
    no chunk of a required evidence type retrieved      <= 0.20, status needs_review
    tier 3: a required evidence type unresolved         <= 0.40, status needs_review
    injection marker in any retrieved chunk             <= 0.60
    embedding or reranker unavailable                   retrieval factor <= 0.60
    unmeasured (embedding, reranker) pair               retrieval factor 0.60, event
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ccp.types import VerdictStatus

UNCALIBRATED_RETRIEVAL = 0.60
DEGRADED_RETRIEVAL_CAP = 0.60
NO_EVIDENCE_CAP = 0.20
UNRESOLVED_CAP = 0.40
INJECTION_CAP = 0.60


@dataclass
class Confidence:
    value: float
    cap: float
    coverage: float
    citation_rate: float
    retrieval_quality: float
    forced_status: VerdictStatus | None = None
    events: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"confidence {self.value:.2f} (cap {self.cap:.2f}; coverage {self.coverage:.2f},"
            f" citations {self.citation_rate:.2f}, retrieval {self.retrieval_quality:.2f})"
        )


def compute(
    *,
    coverage: float,
    citation_rate: float,
    retrieval_quality: float | None,
    no_evidence: bool = False,
    injection: bool = False,
    provider_degraded: bool = False,
    unresolved_evidence_type: bool = False,
) -> Confidence:
    events: list[str] = []
    if retrieval_quality is None:
        retrieval = UNCALIBRATED_RETRIEVAL
        events.append("retrieval_uncalibrated")
    else:
        retrieval = max(0.0, min(1.0, retrieval_quality))
    if provider_degraded and retrieval > DEGRADED_RETRIEVAL_CAP:
        retrieval = DEGRADED_RETRIEVAL_CAP
        events.append("retrieval_degraded_cap")
    cap = 1.0
    forced: VerdictStatus | None = None
    if no_evidence:
        cap = min(cap, NO_EVIDENCE_CAP)
        forced = VerdictStatus.needs_review
        events.append("no_evidence_cap")
    if unresolved_evidence_type:
        cap = min(cap, UNRESOLVED_CAP)
        forced = VerdictStatus.needs_review
        events.append("unresolved_evidence_cap")
    if injection:
        cap = min(cap, INJECTION_CAP)
        events.append("injection_cap")
    value = round(min(cap, max(0.0, coverage) * max(0.0, citation_rate) * retrieval), 4)
    return Confidence(
        value=value,
        cap=cap,
        coverage=round(coverage, 4),
        citation_rate=round(citation_rate, 4),
        retrieval_quality=round(retrieval, 4),
        forced_status=forced,
        events=events,
    )
