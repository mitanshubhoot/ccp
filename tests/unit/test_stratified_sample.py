"""MT10 (A-157): the nightly judge sample is drawn by strata, with the risky ones weighted."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from ccp.evals import relabel
from ccp.evals.relabel import Candidate

THRESHOLD = 0.7


def _pool() -> list[Candidate]:
    out: list[Candidate] = []
    for n in range(30):  # a common stratum: confident failures of one type
        out.append(Candidate(f"f{n:02d}", "HR-3", "fail", 0.95, "training_record"))
    for n in range(10):  # AI-derived passes near the threshold
        out.append(Candidate(f"p{n:02d}", "HR-3", "pass", 0.72, "training_record"))
    for n in range(10):  # confident passes of another type
        out.append(Candidate(f"q{n:02d}", "GH-4", "pass", 0.95, "policy_document"))
    out.append(Candidate("rare", "HR-4", "needs_review", None, "access_review_report"))
    return out


def test_bands_and_weights() -> None:
    assert relabel.band(0.75, THRESHOLD) == "near" and relabel.band(0.6, THRESHOLD) == "near"
    assert relabel.band(0.95, THRESHOLD) == "high" and relabel.band(0.3, THRESHOLD) == "low"
    assert relabel.band(None, THRESHOLD) == "unknown"
    assert relabel.weight("pass|near|x") == 4 and relabel.weight("pass|high|x") == 2
    assert relabel.weight("fail|near|x") == 2 and relabel.weight("fail|high|x") == 1


def test_every_stratum_is_drawn_and_the_risky_ones_more_often() -> None:
    picked = relabel.stratify(_pool(), 12, threshold=THRESHOLD, seed="acme:2026-11-10")
    assert len(picked) == 12
    counts = Counter(key for key, _ in picked)
    assert counts["needs_review|unknown|access_review_report"] == 1  # the rare one is in
    assert counts["pass|near|training_record"] > counts["fail|high|training_record"]
    assert counts["pass|high|policy_document"] > counts["fail|high|training_record"]
    again = relabel.stratify(_pool(), 12, threshold=THRESHOLD, seed="acme:2026-11-10")
    assert [c.verdict_id for _, c in again] == [c.verdict_id for _, c in picked]  # seeded
    other = relabel.stratify(_pool(), 12, threshold=THRESHOLD, seed="acme:2026-11-11")
    assert [c.verdict_id for _, c in other] != [c.verdict_id for _, c in picked]


def test_a_small_pool_is_taken_whole() -> None:
    pool = _pool()[:3]
    assert len(relabel.stratify(pool, 20, threshold=THRESHOLD, seed="s")) == 3
    assert relabel.stratify([], 20, threshold=THRESHOLD, seed="s") == []


def test_the_relabel_month_is_the_one_before() -> None:
    assert relabel.previous_month(datetime(2026, 12, 1, tzinfo=UTC)) == "2026-11"
    assert relabel.previous_month(datetime(2027, 1, 1, tzinfo=UTC)) == "2026-12"
