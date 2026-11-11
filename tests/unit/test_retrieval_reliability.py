"""MT9 (A-148): the reliability fit, gate overrides, and the per-type re-embedding check."""

from __future__ import annotations

from ccp.retrieval import calibration, gates, reembed
from ccp.retrieval.config import reliability_at
from ccp.retrieval.metrics import MetricsReport, QueryOutcome


def test_the_fit_is_monotone_smoothed_and_merges_ties() -> None:
    pairs = [(0.1, False), (0.2, True), (0.3, False), (0.5, True), (0.5, True), (0.9, True)]
    curve = calibration.fit_reliability(pairs, "rerank")
    ps = [b.p for b in curve.bins]
    assert ps == sorted(ps) and curve.samples == 6
    assert [b.max_score for b in curve.bins] == sorted({b.max_score for b in curve.bins})
    assert curve.bins[0].p == round(1 / 3, 4)  # 0 of 1, smoothed: never a flat zero
    assert reliability_at(curve, 0.05) == curve.bins[0].p
    assert reliability_at(curve, 2.0) == curve.bins[-1].p


def test_overrides_merge_default_type_then_model() -> None:
    rules = gates.RetrievalGates(
        types={"policy_document": {"recall_at_5_min": 0.8}},
        models={"m": {"*": {"mrr_min": 0.3}, "policy_document": {"recall_at_5_min": 0.5}}},
    )
    assert rules.for_type("policy_document").recall_at_5_min == 0.8
    hashed = rules.for_type("policy_document", "m")
    assert (hashed.recall_at_5_min, hashed.mrr_min) == (0.5, 0.3)
    assert rules.for_type("training_record", "m").recall_at_5_min == 0.6


def _report(per_type: dict[str, float]) -> MetricsReport:
    return MetricsReport(
        embed_model="m",
        rerank_model=None,
        include_distractors=False,
        iterative_scan=True,
        queries=[
            QueryOutcome(
                id=f"{etype}-{i}",
                recall_at_5=value,
                reciprocal_rank=value,
                ndcg_at_10=value,
                relevant=1,
                retrieved=10,
                top_score=None,
                evidence_type=etype,
            )
            for etype, value in per_type.items()
            for i in range(3)
        ],
    )


def test_a_switch_may_not_make_one_type_worse() -> None:
    serving = _report({"policy_document": 0.6, "training_record": 1.0})
    candidate = _report({"policy_document": 0.9, "training_record": 0.8})  # better overall
    result = reembed.gate(serving, candidate)
    assert not result.passed
    assert result.reasons == [
        "training_record: recall_at_5 0.800 < serving 1.000 - 0.02",
        "training_record: mrr 0.800 < serving 1.000 - 0.02",
    ]
