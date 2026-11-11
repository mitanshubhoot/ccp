"""MT10 (A-152): one code-driven correction round after a failed check, and a repeated tool
call answered from its stored result.

The defect: a citation that failed its check was dropped with no chance of repair, a schema
mismatch was only logged, and a model that repeated a call ran the tool again.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ccp.agent import threads
from ccp.agent.graphs import task_item
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


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, Providers]]:
    with prepared_tenants(base_settings, "ccp_correct", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = Providers(HashingEmbeddingClient(), None, TaskItemStub(), RetrievalConfig())
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        yield settings, acme, providers


def _run(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path, text: str
) -> tuple[store.Task, store.Item]:
    settings, acme, providers = world
    settings = settings.model_copy(update={"ccp_eval_runs_dir": tmp_path})
    task = runner.plan(
        acme,
        settings,
        "questionnaire",
        "c",
        {"questions": [{"id": "x", "text": text}]},
        created_by="me",
        now=NOW,
    )
    runner.advance(
        acme, settings, task.task_id, reader=launcher.reader(settings, providers), now=NOW
    )
    item = store.item(acme, task.task_id, "q:x:read")
    assert item is not None
    return task, item


def _steps(acme: ScopedDB, run_id: str, kind: str) -> list[tuple[str, str]]:
    return [
        (r[0], r[1])
        for r in acme.fetch_all(
            "SELECT node, call_key FROM agent_steps WHERE thread_id = %s AND kind = %s"
            " ORDER BY seq",
            (run_id, kind),
        )
    ]


def test_a_failed_citation_is_repaired_in_one_round(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path
) -> None:
    _, acme, _ = world
    task, item = _run(world, tmp_path, "Repairable: does the policy require multi-factor auth?")
    assert item.status == "final" and item.outcome == "yes"  # the corrected answer merged
    assert item.result is not None and item.result["dropped_claims"] == 0
    thread = threads.get(acme, str(item.last_run_id))
    assert thread is not None and tuple(thread.trajectory) == task_item.TRAJECTORY_CORRECTED
    models = [node for node, _ in _steps(acme, str(item.last_run_id), "model")]
    assert models.count("answer") == 2  # the first answer and its one correction
    finished = [e for e in store.events(acme, task.task_id) if e["kind"] == "run_finished"]
    assert finished[0]["detail"]["corrections"] == 1
    assert finished[0]["detail"]["failed_after_correction"] is False


def test_a_check_that_fails_again_holds_the_item(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path
) -> None:
    _, acme, _ = world
    task, item = _run(world, tmp_path, "Please fabricate: are backups encrypted?")
    assert item.status == "needs_review"
    held = [
        e["detail"]["why"]
        for e in store.events(acme, task.task_id)
        if e["kind"] == "item_status" and e["detail"]["status"] == "needs_review"
    ]
    assert held == ["check_failed_after_correction"]
    thread = threads.get(acme, str(item.last_run_id))
    assert thread is not None and thread.trajectory.count("correct") == 1  # never twice


def test_a_clean_run_takes_no_correction_round(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path
) -> None:
    _, acme, _ = world
    _, item = _run(world, tmp_path, "Does the policy require multi-factor authentication?")
    thread = threads.get(acme, str(item.last_run_id))
    assert thread is not None and tuple(thread.trajectory) == task_item.TRAJECTORY


def test_a_repeated_call_gets_its_stored_result_and_runs_once(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path
) -> None:
    _, acme, _ = world
    _, item = _run(world, tmp_path, "Repeat: does the policy require multi-factor auth?")
    assert item.status == "final"
    tools = _steps(acme, str(item.last_run_id), "tool")
    assert len(tools) == 1  # the second identical search was not run again
    events = acme.fetch_one(
        "SELECT response FROM agent_steps WHERE thread_id = %s AND kind = 'model'"
        " AND node = 'gather' ORDER BY seq DESC LIMIT 1",
        (str(item.last_run_id),),
    )
    assert events is not None
    request = events[0]["request"]
    last_result = request["messages"][-1]["content"][0]["content"]
    assert "repeated_call" in last_result and "stored result" in last_result
