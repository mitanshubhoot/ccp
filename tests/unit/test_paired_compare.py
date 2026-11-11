"""MT9 (A-149): the statistics of a paired retrieval comparison."""

from __future__ import annotations

from ccp.observability import cost
from ccp.retrieval import compare


def test_sign_test_is_exact_and_two_sided() -> None:
    assert compare.sign_test(0, 0) == 1.0
    assert compare.sign_test(5, 0) == 0.0625  # 2 * (1/2)^5
    assert compare.sign_test(3, 3) == 1.0
    assert compare.sign_test(10, 1) == compare.sign_test(1, 10) < 0.05


def test_the_bootstrap_is_reproducible_and_brackets_the_mean() -> None:
    deltas = [0.5, 0.2, 0.0, 0.1, 0.3, -0.1, 0.4, 0.2]
    lo, hi = compare.bootstrap_ci(deltas)
    assert (lo, hi) == compare.bootstrap_ci(deltas)  # a fixed seed
    assert lo <= sum(deltas) / len(deltas) <= hi and lo > 0
    assert compare.bootstrap_ci([]) == (0.0, 0.0)


def _pairs(deltas: list[float]) -> compare.Comparison:
    out = compare.Comparison("base", "cand")
    for i, d in enumerate(deltas):
        out.pairs.append(
            compare.Pair(f"q{i}", "policy_document", 0.5, 0.5 + d, 0.5, 0.5, 10.0, 12.0)
        )
    return out


def test_the_verdict_needs_the_interval_clear_of_zero() -> None:
    assert _pairs([0.5] * 10).summary()["verdict"] == "candidate_better"
    assert _pairs([-0.5] * 10).summary()["verdict"] == "candidate_worse"
    mixed = _pairs([0.5, -0.5, 0.25, -0.25, 0.0]).summary()
    assert mixed["verdict"] == "no_detectable_difference" and mixed["ties"] == 1
    assert mixed["by_type"]["policy_document"]["queries"] == 5
    assert mixed["latency_ms_p50"] == {"baseline": 10.0, "candidate": 12.0}


def test_the_candidate_pair_is_priced() -> None:
    assert cost.estimate_usd("voyage-4-large", input_tokens=1_000_000) == 0.12
    assert cost.estimate_usd("rerank-3", input_tokens=1_000_000) == 0.05
