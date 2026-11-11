"""Context probes (spec.md A-156, report 6.7 "Context probes join the eval suite").

Four probes over a task item run, each squeezing the run's context the way LangChain
stress-tests an agent (every ceiling at a fifth, large results swapped out early), each
graded by code, never by a model:

    planted_fact          a code planted in the question must come back in the reading,
                          and the question must still be in the last call the run made
    reread_by_handle      a swapped-out result is kept whole: read back by its handle it is
                          unchanged, and the reading still rests on a chunk from it
    resume_without_redo   a task advanced again redoes no finished item, and a finished run
                          started again under its own id makes no new model call
    cited_span_positions  facts early, in the middle and late in a document are each cited
                          at the right place (the pointer's offsets fall in that section)

With the stub model they test the harness; with a real model they test the model inside it.
A whole-document read padded to 100K, 200K and 400K tokens waits for its caller (6.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ccp.agent import tools as agent_tools
from ccp.agent.tools import ReadResultArgs, ToolContext
from ccp.config import Settings
from ccp.evaluators.tier2 import Providers
from ccp.llm import context
from ccp.normalize import repository
from ccp.storage.scoped import ScopedDB
from ccp.tasks import launcher, runner, store
from ccp.tasks.runner import ReadOutcome
from ccp.tasks.store import Item, Task

PROBES = ("planted_fact", "reread_by_handle", "resume_without_redo", "cited_span_positions")
SQUEEZE = 0.2  # every ceiling at a fifth of its value
PLANTED = "PROBE-7731"
MFA = "Does the information security policy require multi-factor authentication?"
POSITIONS = {  # question -> the heading of the section that answers it
    "early": ("Does the security policy apply to contractors?", "## 1. Purpose and scope"),
    "middle": (
        "Do changes to production code need review by a second engineer?",
        "## 4. Change management",
    ),
    "late": ("Are repositories scanned for secrets on every push?", "## 7. Secrets"),
}


@dataclass
class ProbeResult:
    name: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)


def _single(providers: Providers) -> runner.Reader:
    """Every probe reads in one run per pass: the probes measure the context, not fan-out."""

    def read(db: ScopedDB, settings: Settings, task: Task, items: list[Item]) -> ReadOutcome:
        return launcher.single(db, settings, task, items, providers, len(items))

    return read


def _task(
    db: ScopedDB, settings: Settings, providers: Providers, questions: dict[str, str]
) -> Task:
    now = datetime.now(UTC)
    task = runner.plan(
        db,
        settings,
        "questionnaire",
        "context probe",
        {"questions": [{"id": k, "text": v} for k, v in questions.items()]},
        created_by="probe",
        now=now,
    )
    runner.advance(db, settings, task.task_id, reader=_single(providers), now=now)
    return task


def _model_requests(db: ScopedDB, run_id: str) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        "SELECT request FROM agent_steps WHERE thread_id = %s AND kind = 'model' ORDER BY seq",
        (run_id,),
    )
    return [dict(r[0]) for r in rows]


def planted_fact(db: ScopedDB, settings: Settings, providers: Providers) -> ProbeResult:
    question = f"Repeat the review code {PLANTED} in your reason. {MFA}"
    with context.squeezed(SQUEEZE, swap_tokens=300, preview_tokens=60):
        task = _task(db, settings, providers, {"planted": question})
    item = store.item(db, task.task_id, "q:planted:read")
    assert item is not None
    requests = _model_requests(db, str(item.last_run_id))
    reading = item.result or {}
    echoed = PLANTED in str(reading.get("reason", ""))
    kept = bool(requests) and PLANTED in str(requests[-1])
    swapped = db.fetch_one(
        "SELECT count(*) FROM tool_results WHERE thread_id = %s", (str(item.last_run_id),)
    )
    return ProbeResult(
        "planted_fact",
        echoed and kept and item.status == "final",
        {
            "status": item.status,
            "echoed": echoed,
            "kept_in_last_call": kept,
            "results_swapped": int(swapped[0]) if swapped else 0,
        },
    )


def reread_by_handle(db: ScopedDB, settings: Settings, providers: Providers) -> ProbeResult:
    with context.squeezed(SQUEEZE, swap_tokens=200, preview_tokens=40):
        task = _task(db, settings, providers, {"handle": MFA})
    item = store.item(db, task.task_id, "q:handle:read")
    assert item is not None
    run_id = str(item.last_run_id)
    rows = db.fetch_all(
        "SELECT handle, content FROM tool_results WHERE thread_id = %s"
        " AND tool = 'search_evidence'",
        (run_id,),
    )
    ctx = ToolContext(db, providers.embedder, providers.reranker, providers.retrieval)
    intact = bool(rows) and all(
        agent_tools.read_result(ctx, ReadResultArgs(handle=h, max_chars=16000))["text"]
        == str(c)[:16000]
        for h, c in rows
    )
    stored_text = " ".join(str(c) for _, c in rows)
    cited = [f["chunk_id"] for f in (item.result or {}).get("findings", [])]
    rests_on_stored = bool(cited) and all(c in stored_text for c in cited)
    reread = db.fetch_one(
        "SELECT count(*) FROM agent_steps WHERE thread_id = %s AND kind = 'tool'"
        " AND request->>'name' = 'read_result'",
        (run_id,),
    )
    return ProbeResult(
        "reread_by_handle",
        intact and rests_on_stored and item.status == "final",
        {
            "stored": len(rows),
            "intact": intact,
            "rests_on_stored": rests_on_stored,
            "reread_by_model": bool(reread and reread[0]),
        },
    )


def resume_without_redo(db: ScopedDB, settings: Settings, providers: Providers) -> ProbeResult:
    task = _task(
        db, settings, providers, {"first": MFA, "second": "Are backups encrypted at rest?"}
    )
    before = {i.item_id: i for i in store.items_of(db, task.task_id)}

    def calls() -> int:
        row = db.fetch_one("SELECT count(*) FROM agent_steps WHERE kind = 'model'")
        return int(row[0]) if row else 0

    count = calls()
    runner.advance(db, settings, task.task_id, reader=_single(providers))
    after = {i.item_id: i for i in store.items_of(db, task.task_id)}
    untouched = all(
        after[k].attempts == before[k].attempts and after[k].last_run_id == before[k].last_run_id
        for k in before
    )
    first = after["q:first:read"]
    current = store.get(db, task.task_id)
    assert current is not None
    again = launcher.run_items(
        db, settings, current, [first], providers, run_id=str(first.last_run_id)
    )
    restarted = store.item(db, task.task_id, "q:first:read")
    same = restarted is not None and restarted.status == first.status
    new_calls = calls() - count
    return ProbeResult(
        "resume_without_redo",
        untouched and new_calls == 0 and again.usd == 0 and same,
        {"untouched": untouched, "new_model_calls": new_calls, "same_outcome": same},
    )


def _sections(text: str) -> list[tuple[str, int]]:
    return [(m.group(0), m.start()) for m in re.finditer(r"^## .+$", text, re.MULTILINE)]


def cited_span_positions(db: ScopedDB, settings: Settings, providers: Providers) -> ProbeResult:
    task = _task(db, settings, providers, {k: q for k, (q, _) in POSITIONS.items()})
    placed: dict[str, bool] = {}
    for key, (_, heading) in POSITIONS.items():
        item = store.item(db, task.task_id, f"q:{key}:read")
        findings = (item.result or {}).get("findings", []) if item else []
        ok = False
        for f in findings:
            evidence = repository.evidence_version(db, str(f.get("evidence_entity_id")))
            if evidence is None or f.get("start_offset") is None:
                continue
            sections = _sections(evidence.text)
            starts = [s for h, s in sections if h == heading]
            if not starts:
                continue
            after = [s for _, s in sections if s > starts[0]]
            end = after[0] if after else len(evidence.text)
            ok = ok or starts[0] <= int(f["start_offset"]) < end
        placed[key] = ok and item is not None and item.status == "final"
    return ProbeResult("cited_span_positions", all(placed.values()), {"placed": placed})


RUNNERS = {
    "planted_fact": planted_fact,
    "reread_by_handle": reread_by_handle,
    "resume_without_redo": resume_without_redo,
    "cited_span_positions": cited_span_positions,
}


def run(name: str, db: ScopedDB, settings: Settings, providers: Providers) -> ProbeResult:
    try:
        probe = RUNNERS[name]
    except KeyError:
        raise ValueError(f"unknown probe {name!r}; known: {list(PROBES)}") from None
    return probe(db, settings, providers)
