"""MT9 (A-149): a candidate embedding model and reranker are compared with the serving pair,
query by query, without switching anything.

The defect: moving to voyage-4-large and rerank-3 had no measured comparison behind it; the
only option was to change the setting and see.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ccp.config import Settings
from ccp.retrieval import compare, generations, index, metrics
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.retrieval.rerank import LexicalOverlapReranker
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db

EMBEDDER = HashingEmbeddingClient()


class Candidate(HashingEmbeddingClient):
    model_id = "local-hashing-v3"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_compare", ("acme",)) as (settings, dbs):
        index.refresh(dbs["acme"], settings, EMBEDDER, contextual_enrichment=False)
        yield settings, dbs["acme"]


def test_a_candidate_is_compared_query_by_query_and_nothing_switches(
    world: tuple[Settings, ScopedDB],
) -> None:
    settings, acme = world
    serving = generations.active(acme)
    assert serving is not None
    built = index.refresh(acme, settings, Candidate(), contextual_enrichment=False)
    assert not built.serving and built.sets_written > 0
    queries = [q for q in metrics.load_queries() if q.tenant == "acme"]
    result = compare.compare(
        acme,
        queries,
        compare.Side(EMBEDDER, None, serving.generation_id),
        compare.Side(Candidate(), LexicalOverlapReranker(), built.generation_id),
    )
    summary = result.summary()
    assert summary["queries"] == len(queries) and summary["unresolved"] == []
    assert summary["baseline"] == "local-hashing-v1|none"
    assert summary["candidate"] == "local-hashing-v3|local-overlap-v1"
    assert summary["wins"] + summary["losses"] + summary["ties"] == len(queries)
    lo, hi = summary["mrr_delta_ci95"]
    assert lo <= summary["mrr_delta"] <= hi
    assert summary["verdict"] in {
        "candidate_better",
        "candidate_worse",
        "no_detectable_difference",
    }
    assert set(summary["by_type"]) == {q.evidence_type for q in queries}
    still = generations.active(acme)
    assert still is not None and still.generation_id == serving.generation_id  # informs only
    same = compare.compare(
        acme,
        queries,
        compare.Side(EMBEDDER, None, serving.generation_id),
        compare.Side(EMBEDDER, None, serving.generation_id),
    ).summary()
    assert same["mrr_delta"] == 0 and same["ties"] == len(queries)
    assert same["verdict"] == "no_detectable_difference"
