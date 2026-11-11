"""MT10 (A-155): harness metrics per graph and bundle, from the run log, and the gate that
keeps a bundle with more loop or cap stops from shipping.

The defect: nothing counted how agent runs ended, how often a tool failed or repeated, or
whether a check passed the first time, so a prompt or tool change that made runs loop or hit
their caps could ship unnoticed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ccp.agent import harness
from ccp.config import Settings
from ccp.evaluators.tier2 import Providers
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from ccp.tasks import launcher, runner
from tests.support.pipeline import prepared_tenants
from tests.support.stub_task_item import TaskItemStub

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, Providers]]:
    with prepared_tenants(base_settings, "ccp_hmetrics", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = Providers(HashingEmbeddingClient(), None, TaskItemStub(), RetrievalConfig())
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        yield settings, acme, providers


def _read(world: tuple[Settings, ScopedDB, Providers], runs: Path, text: str) -> None:
    settings, acme, providers = world
    settings = settings.model_copy(update={"ccp_eval_runs_dir": runs})
    task = runner.plan(
        acme,
        settings,
        "questionnaire",
        "m",
        {"questions": [{"id": "x", "text": text}]},
        created_by="me",
        now=NOW,
    )
    runner.advance(
        acme, settings, task.task_id, reader=launcher.reader(settings, providers), now=NOW
    )


def test_every_finished_run_is_counted_and_the_report_gives_its_rates(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path
) -> None:
    _, acme, _ = world
    _read(world, tmp_path, "Does the policy require multi-factor authentication?")  # clean
    _read(world, tmp_path, "Repairable: does the policy require multi-factor auth?")  # repaired
    _read(world, tmp_path, "Please fabricate: are backups encrypted?")  # fails twice
    _read(world, tmp_path, "Repeat: does the policy require multi-factor auth?")  # repeats
    rows = acme.fetch_all(
        "SELECT graph, first_pass, accepted FROM run_metrics ORDER BY recorded_at"
    )
    assert [r[0] for r in rows] == ["task_item"] * 4
    assert [(r[1], r[2]) for r in rows] == [
        (True, True),
        (False, True),
        (False, False),
        (True, True),
    ]
    (report,) = harness.report(acme, graph="task_item")
    assert report["runs"] == 4
    assert report["first_pass_rate"] == 0.5
    assert report["correction_rounds"] == 2 and report["correction_success_rate"] == 0.5
    search = report["tools"]["search_evidence"]
    assert search["calls"] == 5 and search["repeat_rate"] == 0.2  # one of five was a repeat
    assert report["accepted"] == 3 and report["tokens_per_accepted"] > 0
    assert report["loop_or_cap_stop_rate"] == 0.0
    (fan,) = harness.fanout(acme)
    assert fan["kind"] == "questionnaire" and fan["runs"] == 4 and fan["failed_items"] == 1
    assert fan["held_after_correction"] == 1


def _metrics(acme: ScopedDB, bundle: str, n: int, stopped: int) -> None:
    for i in range(n):
        acme.execute(
            "INSERT INTO run_metrics (tenant_id, thread_id, graph, bundle_id, stops,"
            " model_calls, tool_calls, corrections, correction_succeeded, first_pass,"
            " context_swaps, context_dropped, tokens, usd, accepted)"
            " VALUES ('acme', %s, 'investigator', %s, %s, 2, '{}', 0, NULL, true, 0, 0, 100,"
            " 0.01, %s)",
            (
                f"acme:gate:{bundle}:{i}",
                bundle,
                json.dumps(["loop_repeat_call"] if i < stopped else []),
                i >= stopped,
            ),
        )


def test_a_bundle_with_more_loop_or_cap_stops_does_not_ship(
    world: tuple[Settings, ScopedDB, Providers],
) -> None:
    _, acme, _ = world
    _metrics(acme, "base-1", 10, 1)
    _metrics(acme, "worse-1", 10, 3)
    _metrics(acme, "same-1", 10, 1)
    _metrics(acme, "thin-1", 3, 3)
    (worse,) = harness.gate(acme, "base-1", "worse-1", graph="investigator")
    assert worse.blocks and worse.baseline_rate == 0.1 and worse.candidate_rate == 0.3
    (same,) = harness.gate(acme, "base-1", "same-1", graph="investigator")
    assert same.verdict == "pass"
    (thin,) = harness.gate(acme, "base-1", "thin-1", graph="investigator")
    assert thin.verdict == "insufficient" and not thin.blocks  # too few runs to judge
    (new,) = harness.gate(acme, "never-ran", "worse-1", graph="investigator")
    assert new.verdict == "pass" and new.baseline_rate is None  # nothing to rise from
