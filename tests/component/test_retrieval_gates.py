"""MT9 (A-148): retrieval is gated per evidence type, and confidence reads a measured
reliability curve instead of a flat 0.60.

The defects: one overall Recall@5 over 30 queries hid types with one or two queries each
(access reviews retrieve poorly under the keyless embedder and nothing said so), a distractor
in first place was never counted, and an uncalibrated retrieval factor was a flat 0.60
whatever the top score said.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ccp.config import Settings
from ccp.retrieval import calibration, distractors, gates, index, metrics
from ccp.retrieval.config import RetrievalConfig, retrieval_confidence
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.retrieval.rerank import LexicalOverlapReranker
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

EMBEDDER = HashingEmbeddingClient()
RERANKER = LexicalOverlapReranker()


@pytest.fixture(scope="module")
def report(base_settings: Settings) -> Iterator[metrics.MetricsReport]:
    with prepared_tenants(base_settings, "ccp_rgates") as (settings, dbs):
        runs = []
        for db in dbs.values():
            index.refresh(db, settings, EMBEDDER, contextual_enrichment=False)
            distractors.install(db, EMBEDDER, 300)
            runs.append(
                metrics.evaluate_queries(
                    db,
                    EMBEDDER,
                    metrics.load_queries(),
                    reranker=RERANKER,
                    config=RetrievalConfig(),
                )
            )
        yield metrics.merge(runs)


def test_every_type_is_gated_over_all_its_queries(report: metrics.MetricsReport) -> None:
    assert report.unresolved == [] and len(report.queries) == len(metrics.load_queries())
    outcomes = {o.evidence_type: o for o in gates.check(report)}
    assert set(outcomes) == {
        "policy_document",
        "training_record",
        "access_review_report",
        "critical_systems_list",
        "repo_settings",
        "repo_file_listing",
    }
    assert all(o.gated for o in outcomes.values())  # every type holds three queries or more
    assert gates.blocking(report, list(outcomes.values())) == []  # the stand-in's own floors
    summary = report.summary()
    assert set(summary["by_type"]) == set(outcomes) and "distractor_at_1" in summary


def test_the_production_floors_block_the_weak_type(report: metrics.MetricsReport) -> None:
    production = gates.load().model_copy(update={"models": {}})
    reasons = gates.blocking(report, gates.check(report, production))
    assert reasons and all(r.startswith("access_review_report:") for r in reasons)
    assert any("distractor_at_1" in r for r in reasons)


def test_confidence_reads_the_fitted_curve(report: metrics.MetricsReport) -> None:
    curves = calibration.reliability_curves(report)
    pooled = RetrievalConfig.reliability_key(EMBEDDER.model_id, RERANKER.model_id, "*")
    assert curves[pooled].samples == len(report.queries)
    ps = [b.p for b in curves[pooled].bins]
    scores = [b.max_score for b in curves[pooled].bins]
    assert ps == sorted(ps) and len(set(scores)) == len(scores)  # monotone, no tied bins
    config = calibration.record_reliability(RetrievalConfig(), curves)
    top = max(q.top_score or 0.0 for q in report.queries)
    value, method = retrieval_confidence(
        top, config, EMBEDDER.model_id, RERANKER.model_id, "repo_file_listing"
    )
    assert method == "reliability:pooled" and value is not None and 0 < value <= 1  # 3 < 8
    _, typed = retrieval_confidence(
        top, config, EMBEDDER.model_id, RERANKER.model_id, "policy_document"
    )
    assert typed == "reliability:type"  # 11 policy queries: its own curve
    assert retrieval_confidence(top, RetrievalConfig(), "m", None, "x") == (None, "uncalibrated")
