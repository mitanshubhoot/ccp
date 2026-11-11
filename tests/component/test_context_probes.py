"""MT10 (A-156): the four context probes, on the stub model, so the harness they measure is
checked on every run; with a key, the same probes run as golden cases on the real model.

The defect: nothing tested whether a squeezed context kept the question, whether a swapped
result could be read back whole, whether resuming redid finished work, or whether a fact's
place in a document changed whether it was cited.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ccp.config import Settings
from ccp.evals import probes
from ccp.evaluators.tier2 import Providers
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants
from tests.support.stub_task_item import TaskItemStub

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, Providers]]:
    with prepared_tenants(base_settings, "ccp_probes", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = Providers(HashingEmbeddingClient(), None, TaskItemStub(), RetrievalConfig())
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        yield settings, acme, providers


@pytest.mark.parametrize("name", probes.PROBES)
def test_each_context_probe_passes_on_the_harness(
    world: tuple[Settings, ScopedDB, Providers], name: str
) -> None:
    settings, acme, providers = world
    result = probes.run(name, acme, settings, providers)
    assert result.passed, result.detail


def test_the_probes_report_what_they_saw(world: tuple[Settings, ScopedDB, Providers]) -> None:
    settings, acme, providers = world
    planted = probes.planted_fact(acme, settings, providers)
    assert planted.detail["results_swapped"] >= 1  # the run was squeezed, and still kept it
    reread = probes.reread_by_handle(acme, settings, providers)
    assert reread.detail["stored"] >= 1 and reread.detail["reread_by_model"]
    resumed = probes.resume_without_redo(acme, settings, providers)
    assert resumed.detail["new_model_calls"] == 0
    placed = probes.cited_span_positions(acme, settings, providers)
    assert placed.detail["placed"] == {"early": True, "middle": True, "late": True}
    with pytest.raises(ValueError, match="unknown probe"):
        probes.run("nope", acme, settings, providers)


def test_the_eval_target_runs_a_probe_and_reports_it(
    world: tuple[Settings, ScopedDB, Providers],
) -> None:
    from ccp.evals.targets import TARGETS, EvalContext

    settings, _, providers = world
    ctx = EvalContext(settings=settings, db_settings=settings, providers=providers)
    try:
        out = TARGETS["context_probe"](ctx, "acme", {"probe": "resume_without_redo"})
    finally:
        ctx.close()
    assert out["probe"] == "resume_without_redo" and out["passed"] is True
