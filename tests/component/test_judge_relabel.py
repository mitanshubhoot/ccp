"""MT10 (A-157): the nightly judged sample is stratified and recorded item by item, and each
month an expert relabels 20 judged items; agreement outside the judge's calibrated interval
fails the check and asks for re-calibration.

The defect: the nightly sample was the 20 newest verdicts, so a rare stratum or a risky
near-threshold PASS could go unjudged for weeks, and nothing compared the judge with an
expert after its one calibration.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ccp.config import Settings
from ccp.evals import production, relabel
from ccp.evaluators import runner as evaluations
from ccp.evaluators.tier2 import Providers
from ccp.llm.client import StubLLMClient
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from tests.support.pipeline import prepared_tenants
from tests.support.stub_extractors import stub_extract

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_relabel", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = Providers(
            HashingEmbeddingClient(), None, StubLLMClient(handler=stub_extract), RetrievalConfig()
        )
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-3", "GH-4"))
        evaluations.drain(acme, settings, now=NOW, providers=providers)
        yield settings, acme


@pytest.fixture
def judge(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A judge that fails every third verdict it is shown."""
    seen: list[str] = []

    def fake(llm: Any, rubric: Any, **kw: Any) -> SimpleNamespace:
        status = kw["output"]["status"]
        seen.append(status)
        return SimpleNamespace(overall_pass=len(seen) % 3 != 0)

    monkeypatch.setattr(production, "try_build_client", lambda settings: object())
    monkeypatch.setattr(production.judging, "judge", fake)
    return seen


def test_the_nightly_sample_is_stratified_seeded_and_recorded(
    world: tuple[Settings, ScopedDB], judge: list[str], tmp_path: Path
) -> None:
    settings, acme = world
    settings = settings.model_copy(update={"ccp_eval_runs_dir": tmp_path})
    slot = NOW + timedelta(hours=1)
    candidates = acme.fetch_one("SELECT count(*) FROM verdicts WHERE tier > 1")
    assert candidates is not None and candidates[0] > 0
    judged = production.judged_sample(acme, settings, slot)
    assert judged == min(int(candidates[0]), production.SAMPLE)
    rows = acme.fetch_all("SELECT check_id, verdict_id, stratum FROM judged_items")
    assert len(rows) == judged and len({r[0] for r in rows}) == 1
    check = acme.fetch_one(
        "SELECT check_id, measured FROM quality_checks WHERE kind = 'judged_sample'"
    )
    assert check is not None and check[0] == rows[0][0]  # the items belong to the check
    measured = check[1]
    assert sum(s["judged"] for s in measured["strata"].values()) == judged
    assert {r[2] for r in rows} == set(measured["strata"])
    first = sorted(r[1] for r in rows)
    production.judged_sample(acme, settings, slot)  # the same night again: the same draw
    second = acme.fetch_all(
        "SELECT verdict_id FROM judged_items WHERE check_id <> %s", (rows[0][0],)
    )
    assert sorted(r[0] for r in second) == first


def test_the_monthly_relabel_checks_the_judge_against_its_interval(
    world: tuple[Settings, ScopedDB], judge: list[str], tmp_path: Path
) -> None:
    settings, acme = world
    settings = settings.model_copy(update={"ccp_eval_runs_dir": tmp_path})
    for hours in (2, 3):  # enough judged items for a batch
        production.judged_sample(acme, settings, NOW + timedelta(hours=hours))
    now = acme.fetch_one("SELECT clock_timestamp()")
    assert now is not None
    first_of_next = (now[0].replace(day=1) + timedelta(days=32)).replace(day=1, hour=5)
    assert relabel.monthly_batch(acme, first_of_next - timedelta(days=3)) == 0  # not the 1st
    made = relabel.monthly_batch(acme, first_of_next)
    assert made == relabel.RELABEL_SIZE
    assert relabel.monthly_batch(acme, first_of_next) == 0  # once a month
    month = relabel.previous_month(first_of_next)
    items = relabel.batch(acme, month)
    disagreements = [i for i in items if not i["judge_pass"]]
    assert items[: len(disagreements)] == disagreements  # the judge's dissents come first

    (tmp_path / "calibration-20260901T000000.json").write_text(
        json.dumps({"overall": {"agreement": 0.9, "ci_low": 0.8, "ci_high": 1.0}}),
        encoding="utf-8",
    )
    for i in items[:5]:
        relabel.label(acme, i["item_id"], passed=i["judge_pass"], by="expert")
    assert relabel.check(acme, month, tmp_path, "b1")["decided"] is False  # too few labels
    for i in items[5:]:
        relabel.label(acme, i["item_id"], passed=i["judge_pass"], by="expert")
    agreed = relabel.check(acme, month, tmp_path, "b1")
    assert agreed["agreement"] == 1.0 and agreed["inside_interval"] and not agreed["recalibrate"]
    for i in items[:6]:  # the expert changes their mind on six: 70%, under the interval
        relabel.label(acme, i["item_id"], passed=not i["judge_pass"], by="expert", note="no")
    drifted = relabel.check(acme, month, tmp_path, "b1")
    assert drifted["agreement"] == 0.7 and drifted["recalibrate"]
    recorded = acme.fetch_all(
        "SELECT passed FROM quality_checks WHERE kind = 'relabel' ORDER BY recorded_at"
    )
    assert [r[0] for r in recorded] == [True, False]
    with pytest.raises(ValueError, match="no judged item"):
        relabel.label(acme, "nope", passed=True, by="expert")
