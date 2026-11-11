"""MT10 (A-153): a large tool result is kept whole and read back by its handle, and a call
that would pass its ceiling is not sent.

The defect: a tool result over 20,000 characters was cut and the rest was gone for the run,
and nothing bounded what one model call of an agent could carry.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ccp.agent import tools as agent_tools
from ccp.agent.tools import ReadResultArgs, ToolContext
from ccp.config import Settings
from ccp.evaluators.tier2 import Providers
from ccp.llm import context
from ccp.retrieval import index
from ccp.retrieval.config import RetrievalConfig
from ccp.retrieval.embeddings import HashingEmbeddingClient
from ccp.storage.scoped import ScopedDB
from ccp.tasks import launcher, runner, store
from tests.support.pipeline import prepared_tenants
from tests.support.stub_task_item import TaskItemStub

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
MFA = "Does the policy require multi-factor authentication?"


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB, Providers]]:
    with prepared_tenants(base_settings, "ccp_ctxview", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        providers = Providers(HashingEmbeddingClient(), None, TaskItemStub(), RetrievalConfig())
        index.refresh(acme, settings, providers.embedder, contextual_enrichment=False)
        yield settings, acme, providers


def _limits(monkeypatch: pytest.MonkeyPatch, **changes: int) -> None:
    base = context.load()
    by_call = dict(base.by_call)
    for key in ("agent_first_call", "agent_later_call"):
        if key in changes:
            by_call[key] = changes[key]
    limits = context.Ceilings(
        by_call,
        changes.get("swap_tokens", base.swap_tokens),
        changes.get("preview_tokens", base.preview_tokens),
    )
    monkeypatch.setattr(context, "load", lambda path=None: limits)


def _run(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path, text: str = MFA
) -> store.Item:
    settings, acme, providers = world
    settings = settings.model_copy(update={"ccp_eval_runs_dir": tmp_path})
    task = runner.plan(
        acme,
        settings,
        "questionnaire",
        "ctx",
        {"questions": [{"id": "x", "text": text}]},
        created_by="me",
        now=NOW,
    )
    runner.advance(
        acme, settings, task.task_id, reader=launcher.reader(settings, providers), now=NOW
    )
    item = store.item(acme, task.task_id, "q:x:read")
    assert item is not None
    return item


def _tool_steps(acme: ScopedDB, run_id: str) -> list[dict]:  # type: ignore[type-arg]
    return [
        r[0]
        for r in acme.fetch_all(
            "SELECT request FROM agent_steps WHERE thread_id = %s AND kind = 'tool' ORDER BY seq",
            (run_id,),
        )
    ]


def test_a_large_result_is_stored_whole_and_read_back_by_handle(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, acme, providers = world
    _limits(monkeypatch, swap_tokens=200)  # any search result is "large" here
    item = _run(world, tmp_path)
    assert item.status == "final" and item.outcome == "yes"  # the answer still had its evidence
    run_id = str(item.last_run_id)
    rows = acme.fetch_all(
        "SELECT handle, tool, content, tokens FROM tool_results WHERE thread_id = %s"
        " AND tool = 'search_evidence'",
        (run_id,),
    )
    assert len(rows) == 1 and rows[0][1] == "search_evidence" and rows[0][3] > 200
    handle, _, content, _ = rows[0]
    assert json.loads(content)["result"]["chunks"]  # kept whole
    calls = [step["name"] for step in _tool_steps(acme, run_id)]
    assert calls == ["search_evidence", "read_result"]  # the run read more by its handle
    ctx = ToolContext(acme, HashingEmbeddingClient(), None, RetrievalConfig())
    first = agent_tools.read_result(ctx, ReadResultArgs(handle=handle, max_chars=200))
    assert first["text"] == content[:200] and first["next_offset"] == 200
    assert first["total_chars"] == len(content)
    last = agent_tools.read_result(
        ctx, ReadResultArgs(handle=handle, offset=len(content) - 50, max_chars=200)
    )
    assert last["next_offset"] is None and last["text"] == content[-50:]
    assert agent_tools.read_result(ctx, ReadResultArgs(handle="nope")) == {"error": "not found"}


def test_a_call_over_its_ceiling_is_not_sent_and_the_item_is_held(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, acme, _ = world
    _limits(monkeypatch, agent_first_call=50)  # below the brief and the tools themselves
    item = _run(world, tmp_path)
    assert item.status == "needs_review"
    stops = [
        e["detail"]["why"]
        for e in store.events(acme, item.task_id)
        if e["kind"] == "item_status" and e["item_id"] == "q:x:read"
    ]
    assert stops[-1] == "stopped:context_ceiling"
    assert _tool_steps(acme, str(item.last_run_id)) == []  # nothing was called


def test_older_results_are_swapped_out_to_keep_a_later_call_under_its_ceiling(
    world: tuple[Settings, ScopedDB, Providers], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, acme, _ = world
    _limits(monkeypatch, agent_later_call=3_000, preview_tokens=100)  # too small to carry it
    item = _run(world, tmp_path)
    run_id = str(item.last_run_id)
    stored = acme.fetch_all(
        "SELECT tool FROM tool_results WHERE thread_id = %s ORDER BY created_at", (run_id,)
    )
    tools = [r[0] for r in stored]
    assert tools[0] == "search_evidence"  # swapped out to make room, then read back by handle
    last = acme.fetch_one(
        "SELECT response FROM agent_steps WHERE thread_id = %s AND kind = 'model'"
        " AND node = 'gather' ORDER BY seq DESC LIMIT 1",
        (run_id,),
    )
    assert last is not None
    sent = last[0]["request"]["messages"]
    assert '\\"stored\\": true' in json.dumps(sent) or '"stored": true' in json.dumps(sent)
    assert context.estimate_tokens(sent) < 3_000
