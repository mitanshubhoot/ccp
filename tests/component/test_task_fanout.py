"""MT10 (A-151): a long task's reads run as fresh, typed runs, fanned out only after a
single-run baseline, and merged by item id after code checks.

The defect: breadth work had no safe way to run in parallel. There was no per-item run, no
typed result, no merge that a code check stood in front of, and nothing to stop one failing
item from sinking the whole task.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ccp.agent import threads
from ccp.config import Settings
from ccp.evaluators.tier2 import Providers
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from ccp.tasks import launcher, runner, store
from tests.support.pipeline import prepared_tenants
from tests.support.stub_task_item import TaskItemStub

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
MFA = {"id": "mfa", "text": "Does the policy require multi-factor authentication?"}
BACKUPS = {"id": "backups", "text": "Are backups encrypted at rest?"}


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, Providers]]:
    with prepared_tenants(base_settings, "ccp_fanout", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = Providers(
            embedder=HashingEmbeddingClient(),
            reranker=None,
            llm=TaskItemStub(),
            retrieval=RetrievalConfig(),
        )
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        yield settings, acme, providers


def _with_runs(settings: Settings, runs: Path) -> Settings:
    return settings.model_copy(update={"ccp_eval_runs_dir": runs})


def _baseline(runs: Path, kind: str = "questionnaire") -> None:
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"task_measure-{kind}-single-20260901T000000.json").write_text(
        json.dumps({"kind": kind, "mode": "single", "accuracy": 0.5}), encoding="utf-8"
    )


def _plan(settings: Settings, acme: ScopedDB, *questions: dict[str, str]) -> store.Task:
    return runner.plan(
        acme,
        settings,
        "questionnaire",
        "reads",
        {"questions": list(questions)},
        created_by="me",
        now=NOW,
    )


def test_without_a_baseline_open_reads_run_together_in_one_run(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path
) -> None:
    settings, acme, providers = world
    settings = _with_runs(settings, tmp_path)
    assert not launcher.fanout_enabled(settings, "questionnaire")
    task = _plan(settings, acme, MFA, BACKUPS)
    out = runner.advance(
        acme, settings, task.task_id, reader=launcher.reader(settings, providers), now=NOW
    )
    assert out.ran == 1 and out.changed == 2 and out.usd > 0
    mfa = store.item(acme, task.task_id, "q:mfa:read")
    backups = store.item(acme, task.task_id, "q:backups:read")
    assert mfa is not None and mfa.status == "final" and mfa.outcome == "yes"
    (finding,) = mfa.result["findings"]  # type: ignore[index]
    assert (
        isinstance(finding["start_offset"], int) and finding["end_offset"] > finding["start_offset"]
    )
    assert mfa.evidence_ids == (finding["evidence_entity_id"],)
    assert backups is not None and backups.status == "waiting"
    assert backups.open_question == "a backup encryption standard"
    assert mfa.last_run_id == backups.last_run_id  # one run read both
    assert mfa.attempts == backups.attempts == 1
    spent = store.get(acme, task.task_id)
    assert spent is not None and spent.spent_usd == pytest.approx(out.usd)
    finished = [e for e in store.events(acme, task.task_id) if e["kind"] == "run_finished"]
    assert len(finished) == 2 and sum(e["usd"] for e in finished) == pytest.approx(
        out.usd, abs=1e-5
    )


def test_fanout_runs_one_fresh_run_per_item_and_a_failed_item_is_held_alone(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path
) -> None:
    settings, acme, providers = world
    settings = _with_runs(settings, tmp_path)
    _baseline(tmp_path)
    assert launcher.fanout_enabled(settings, "questionnaire")
    task = _plan(
        settings,
        acme,
        MFA,
        {"id": "made", "text": "Please fabricate: are backups encrypted?"},
        {"id": "boom", "text": "This one will explode"},
        {"id": "spooky", "text": "A ghost question about multi-factor authentication"},
    )
    out = runner.advance(
        acme, settings, task.task_id, reader=launcher.reader(settings, providers), now=NOW
    )
    assert out.ran == 4 and {i.status for i in store.items_of(acme, task.task_id)} == {"running"}
    merged = dict(launcher.drain(acme, settings, providers))
    assert merged == {
        "q:mfa:read": "final",
        "q:made:read": "needs_review",  # its only claim quoted nothing real
        "q:boom:read": "needs_review",  # the run failed; the others went on
        "q:spooky:read": "final",
    }
    made = store.item(acme, task.task_id, "q:made:read")
    assert made is not None and made.result["dropped_claims"] == 1  # type: ignore[index]
    spooky = store.item(acme, task.task_id, "q:spooky:read")
    assert spooky is not None
    assert spooky.result["rejections"] == []  # type: ignore[index]
    rejected = [
        e
        for e in store.events(acme, task.task_id)
        if e["kind"] == "run_finished" and e["item_id"] == "q:spooky:read"
    ]
    assert rejected[0]["detail"]["rejections"] == 1  # the ghost reading never merged
    runs = {i.item_id: i.last_run_id for i in store.items_of(acme, task.task_id)}
    assert len(set(runs.values())) == 4  # one run each
    started = threads.input_of(acme, str(runs["q:mfa:read"]))
    assert started is not None
    assert [i["item_id"] for i in started["task_items"]] == ["q:mfa:read"]  # its item alone
    assert "messages" not in started  # a fresh context: no parent history


def test_an_item_at_its_run_limit_is_held_for_a_person(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path
) -> None:
    settings, acme, providers = world
    settings = _with_runs(settings, tmp_path)
    _baseline(tmp_path)
    task = _plan(settings, acme, BACKUPS)
    store.set_item(acme, task.task_id, "q:backups:read", status="open", why="test", attempts=3)
    out = runner.advance(
        acme, settings, task.task_id, reader=launcher.reader(settings, providers), now=NOW
    )
    held = store.item(acme, task.task_id, "q:backups:read")
    assert out.ran == 0 and held is not None and held.status == "needs_review"
    assert [e["detail"]["why"] for e in store.events(acme, task.task_id) if e["item_id"]][-1] == (
        "runs_exhausted"
    )


def test_without_a_model_key_reads_wait_and_nothing_is_reported_as_read(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path
) -> None:
    settings, acme, providers = world
    settings = _with_runs(settings, tmp_path)
    keyless = Providers(providers.embedder, None, None, providers.retrieval)
    task = _plan(settings, acme, MFA)
    out = runner.advance(
        acme, settings, task.task_id, reader=launcher.reader(settings, keyless), now=NOW
    )
    assert "reads_parked:no_model_key" in out.events and out.ran == 0
    found = store.item(acme, task.task_id, "q:mfa:read")
    assert found is not None and found.status == "open" and found.attempts == 0


def test_measure_records_the_single_run_baseline_that_unlocks_fanout(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path
) -> None:
    settings, acme, providers = world
    settings = _with_runs(settings, tmp_path)
    questions = [{**MFA, "expected": "yes"}, {**BACKUPS, "expected": "no"}]
    record = launcher.measure(
        acme, settings, "questionnaire", questions, mode="single", providers=providers, now=NOW
    )
    assert record["items"] == 2 and record["correct"] == 1 and record["accuracy"] == 0.5
    assert record["usd"] > 0 and record["mode"] == "single"
    assert launcher.fanout_enabled(settings, "questionnaire")
    assert not launcher.fanout_enabled(settings, "audit_prep")  # baselines are per kind
