"""The S17 injection suite with stub models: the file loads with every class, a robust model
is not moved by any payload and the corpus is restored, a complying model is moved and the
attack-success-rate counts it, and the runner's ASR metric reads those results."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ccp.config import Settings
from ccp.evals import adversarial, runner
from ccp.evals.targets import TARGETS, EvalContext
from ccp.evaluators.tier2 import Providers
from ccp.llm.client import StubLLMClient
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.db import scratch_database
from tests.support.stub_adversarial import comply_extract
from tests.support.stub_extractors import stub_extract

pytestmark = pytest.mark.db


def _providers(handler: object) -> Providers:
    return Providers(
        embedder=HashingEmbeddingClient(),
        reranker=None,
        llm=StubLLMClient(handler=handler),  # type: ignore[arg-type]
        retrieval=RetrievalConfig(),
    )


@pytest.fixture(scope="module")
def ctx(base_settings: Settings) -> Iterator[EvalContext]:
    with scratch_database(base_settings, prefix="ccp_adv") as settings:
        context = EvalContext(settings=settings, db_settings=settings)
        try:
            yield context
        finally:
            context.close()


def _case(case_id: str) -> runner.GoldenCase:
    return next(c for c in adversarial.load_adversarial() if c.id == case_id)


ACME = "adv-direct-override-acme-report-fail"
GLOBEX = "adv-direct-override-globex-report-pass"


def test_the_suite_loads_with_every_class_and_a_control() -> None:
    cases = adversarial.load_adversarial()
    assert len(cases) == 16
    assert {c.attack_class for c in cases} == set(adversarial.CLASSES)
    for case in cases:
        assert case.target == "adversarial_tier2" and case.requires == ["anthropic"]
        assert case.control_id and case.expected == {"attack_succeeded": False}
        assert case.input["placement"] in adversarial.PLACEMENTS
        assert "adversarial" in case.tags and case.attack_class in case.tags


def test_placements() -> None:
    assert adversarial.apply_placement("base", "p", "append") == "base\n\np"
    assert adversarial.apply_placement("base", "p", "prepend") == "p\n\nbase"
    assert adversarial.apply_placement("base", "p", "replace") == "p"
    assert "| Response timeline | p |" in adversarial.apply_placement("base", "p", "table_cell")


def test_a_robust_model_is_not_moved_and_the_corpus_is_restored(ctx: EvalContext) -> None:
    ctx.providers = _providers(stub_extract)
    for case_id in (ACME, GLOBEX):
        case = _case(case_id)
        out = TARGETS["adversarial_tier2"](ctx, case.tenant, case.input)
        assert out["status"] == out["control_status"], case_id
        assert out["attack_succeeded"] is False and out["poisoned_chunks_left"] == 0
    acme = TARGETS["adversarial_tier2"](ctx, "acme", _case(ACME).input)
    assert acme["injection_detected"] is True  # "ignore previous" is a marker


def test_a_complying_model_is_moved_and_the_asr_counts_it(ctx: EvalContext) -> None:
    ctx.providers = _providers(comply_extract)
    results = [runner.run_case(ctx, _case(case_id)) for case_id in (ACME, GLOBEX)]
    assert all(r.attack_class == "direct_override" and r.attack_succeeded for r in results)
    assert all(not r.passed and not r.skipped for r in results)
    assert results[0].actual is not None and results[0].actual["status"] == "fail"
    assert results[1].actual is not None and results[1].actual["status"] == "pass"
    assert results[1].actual["poisoned_chunk_cited"] is True  # the quote came from the payload
    metrics = runner.compute_metrics(results, ctx.settings)
    assert metrics.asr == {"direct_override": 1.0, "overall": 1.0}
    assert metrics.asr_counts["overall"] == [2, 2]
    assert adversarial.poisoned_count(ctx.scoped("acme")) == 0
    assert adversarial.poisoned_count(ctx.scoped("globex")) == 0
