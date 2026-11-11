"""The fan-out launcher (spec.md A-151, report 6.8 "The fan-out contract").

    open read items ──► fanout? ──yes──► one queued run per item on `task_items` (parallel),
                          │               keyed tenant:task:item, the attempt in its run id;
                          │               a worker runs it in a fresh context and merges
                          no
                          └────────────► one run over up to `single_run_items` items, inline:
                                          the single-run baseline a fan-out must beat

    a run       the task_item graph over typed inputs (the task, the item, its facts and
                handles, the as-of date), a fixed read-only tool list, its own budget (the
                run cap, lowered to what the task has left), a typed reading per item
    the merge   code, by item id: no reading or a stopped loop -> needs_review; an answer of
                insufficient_evidence -> waiting, with what would settle it as the open
                question; an answer with no verified claim -> needs_review; otherwise final.
                A failed item is held alone and never blocks the rest
    the gate    a kind fans out only when config/task_kinds.yaml says so and a single-run
                baseline of it is recorded in evals/runs/ (`ccp tasks measure`)

Only code starts runs: no tool lets a model start one, and a run sees no other run's state.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ccp.agent import authz, core, stops, threads
from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.graphs import task_item as graph_module
from ccp.agent.tools import ToolContext, bind_tools
from ccp.config import REPO_ROOT, Settings
from ccp.evaluators import budget_gate, jobs
from ccp.evaluators.tier2 import AITierUnavailable, Providers
from ccp.ingest import event_log
from ccp.ingest.consumers import ParallelConsumer, ParkedError, RetryableError, TerminalError
from ccp.ingest.event_log import LogRecord
from ccp.ingest.worker import register
from ccp.llm import release
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.errors import BudgetExceeded, RunStopped
from ccp.observability import steps
from ccp.storage import checkpoints
from ccp.storage.db import agent_pool
from ccp.storage.scoped import ScopedDB
from ccp.tasks import runner, store
from ccp.tasks.runner import Reader, ReadOutcome
from ccp.tasks.store import Item, Task

GRAPH = graph_module.GRAPH_NAME
TOPIC = "task_items"
KINDS_FILE = REPO_ROOT / "config" / "task_kinds.yaml"
MEASURE_PREFIX = "task_measure"


@dataclass(frozen=True)
class KindsConfig:
    single_run_items: int
    fanout: dict[str, bool]


def load_kinds(path: Path = KINDS_FILE) -> KindsConfig:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    loaded = loaded or {}
    kinds = loaded.get("kinds") or {}
    return KindsConfig(
        single_run_items=int(loaded.get("single_run_items", 10)),
        fanout={str(k): bool((v or {}).get("fanout")) for k, v in kinds.items()},
    )


def baselines(runs_dir: Path, kind: str) -> list[dict[str, Any]]:
    """The single-run measurements of a kind, newest last."""
    found = sorted(runs_dir.glob(f"{MEASURE_PREFIX}-{kind}-single-*.json"))
    return [json.loads(p.read_text(encoding="utf-8")) for p in found]


def fanout_enabled(settings: Settings, kind: str, config: KindsConfig | None = None) -> bool:
    config = config or load_kinds()
    return config.fanout.get(kind, False) and bool(baselines(settings.ccp_eval_runs_dir, kind))


@dataclass
class RunResult:
    run_id: str
    usd: float
    tokens: int
    merged: dict[str, str] = field(default_factory=dict)  # item id -> status it merged to
    rejections: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


def run_id_for(db: ScopedDB, task: Task, label: str, attempt: int) -> str:
    """tenant:task:<task>/<item or batch>:run<n>: a retried job resumes its own run."""
    return checkpoints.thread_id_for(db.tenant, "task", f"{task.task_id}/{label}", f"run{attempt}")


def _items_payload(items: list[Item]) -> list[dict[str, Any]]:
    return [
        {
            "item_id": i.item_id,
            "question": i.question,
            "inputs": i.inputs,
            "evidence_ids": list(i.evidence_ids),
        }
        for i in items
    ]


def run_items(
    db: ScopedDB,
    settings: Settings,
    task: Task,
    items: list[Item],
    providers: Providers,
    *,
    run_id: str,
    now: datetime | None = None,
) -> RunResult:
    """One fresh run of the task_item graph over these items, then the merge by item id."""
    if providers.llm is None:
        raise AITierUnavailable("ANTHROPIC_API_KEY is not set; task reads are unavailable")
    moment = now or datetime.now(UTC)
    budget_gate.check_daily_budget(db, moment)  # ParkedError past the tenant's day
    remaining = max(0.0, task.max_usd - task.spent_usd)
    tracker = BudgetTracker(RunBudget.from_settings(settings).lowered(max_usd=remaining))
    tracker.stop = stops.watcher(db, run_id)
    previous = threads.get(db, run_id)
    if previous is None:
        threads.create(db, run_id, GRAPH, f"task:{task.kind}", ",".join(i.item_id for i in items))
    else:
        threads.mark_running(db, run_id)
    agent = agent_pool(settings)
    pool = checkpoints.saver_pool(settings, db.tenant)
    trajectory: list[str] = list(previous.trajectory) if previous is not None else []
    try:
        tools = bind_tools(
            ToolContext(
                ScopedDB(agent, db.tenant),
                providers.embedder,
                providers.reranker,
                providers.retrieval,
            ),
            tuple(sorted(ALLOWLISTS[GRAPH])),
            gate=authz.Gate(f"graph:{GRAPH}", run_id=run_id, log_db=db),
        )
        deps = core.ModelDeps(
            providers.llm, settings.task_model, tracker, reasoning=True, log_db=db
        )
        graph = core.build_graph(
            graph_module.build_spec(db.tenant, tools, deps),
            checkpoints.tenant_saver(pool, db.tenant),
        )
        config: Any = {"configurable": {"thread_id": run_id}}
        initial: dict[str, Any] = {
            "tenant_id": db.tenant.tenant_id,
            "thread_id": run_id,
            "now": moment.isoformat(),
            "model": settings.task_model,
            "task": {
                "task_id": task.task_id,
                "kind": task.kind,
                "as_of": task.scope.get("period_end"),
            },
            "task_items": _items_payload(items),
            "retrieved_chunks": {},
            "evidence_coverage": {},
            "extracted_raw": {},
            "extracted": {},
            "requests": {},
            "context": {},
        }
        threads.set_input(db, run_id, initial)
        with steps.step(
            "task.read",
            layer="tasks",
            tenant=db.tenant,
            inputs={"task_id": task.task_id, "items": [i.item_id for i in items], "run_id": run_id},
        ) as s:
            checkpointed = previous is not None and bool(graph.get_state(config).values)
            payload = None if checkpointed else initial
            for update in graph.stream(payload, config, stream_mode="updates", durability="sync"):
                trajectory.extend(str(k) for k in update)
            final = dict(graph.get_state(config).values.get("final") or {})
            threads.mark_finished(db, run_id, task.task_id, trajectory)
            budget_gate.record_spend(db, tracker.usd, tracker.tokens, s.step_id, moment)
            s.set_output({"trajectory": trajectory, "usd": round(tracker.usd, 6)})
    except RunStopped as exc:
        threads.mark_cancelled(db, run_id, str(exc), trajectory)
        raise
    except Exception as exc:
        threads.mark_failed(db, run_id, f"{type(exc).__name__}: {exc}", trajectory)
        raise
    finally:
        pool.close()
        agent.close()
    result = RunResult(run_id, tracker.usd, tracker.tokens)
    result.rejections = list(final.get("rejections") or [])
    result.events = list(final.get("guardrail_events") or [])
    store.charge(db, task.task_id, tracker.usd)
    share = tracker.usd / len(items) if items else 0.0
    for it in items:
        result.merged[it.item_id] = merge(db, task, it, final, run_id, share)
    return result


def merge(
    db: ScopedDB, task: Task, item: Item, final: dict[str, Any], run_id: str, usd: float
) -> str:
    """One item's reading into its row, after the code checks; returns the status."""
    reading = (final.get("readings") or {}).get(item.item_id)
    stopped = final.get("stopped")
    rejections = [r for r in final.get("rejections") or [] if r.endswith(f":{item.item_id}")]
    detail = {
        "run_id": run_id,
        "stopped": stopped,
        "rejections": len(final.get("rejections") or []),
        "items_in_run": len(final.get("readings") or {}) or None,
        "corrections": int(final.get("corrections") or 0),  # A-152
        "failed_after_correction": item.item_id in (final.get("failed_items") or []),
        "model": final.get("model"),
        "bundle_sha256": final.get("bundle_sha256"),
    }
    store.event(
        db, task.task_id, "run_finished", detail, item_id=item.item_id, run_id=run_id, usd=usd
    )
    if reading is None:
        status, why, outcome = "needs_review", "no_reading", None
    elif stopped:
        status, why, outcome = "needs_review", f"stopped:{stopped}", reading["answer"]
    elif item.item_id in (final.get("failed_items") or []):  # failed again after a round
        status, why, outcome = "needs_review", "check_failed_after_correction", reading["answer"]
    elif reading["answer"] == "insufficient_evidence":
        status, why, outcome = "waiting", "insufficient_evidence", reading["answer"]
    elif not reading["findings"]:
        status, why, outcome = "needs_review", "unsupported", reading["answer"]
    else:
        status, why, outcome = "final", "read and checked", reading["answer"]
    evidence = sorted(
        {
            str(f.get("evidence_entity_id") or f["chunk_id"])
            for f in (reading or {}).get("findings", [])
        }
    )
    open_question = None
    if status == "waiting" and reading is not None:
        open_question = "; ".join(reading["missing"]) or "the evidence does not answer this"
    store.set_item(
        db,
        task.task_id,
        item.item_id,
        status=status,  # type: ignore[arg-type]
        why=why,
        outcome=outcome,
        result=(reading or {}) | {"rejections": rejections, "run_id": run_id},
        evidence_ids=evidence,
        open_question=open_question,
        last_run_id=run_id,
        run_id=run_id,
    )
    return status


def _hold(db: ScopedDB, task: Task, item: Item, run_id: str, reason: str) -> None:
    store.set_item(
        db,
        task.task_id,
        item.item_id,
        status="needs_review",
        why=reason,
        last_run_id=run_id,
        run_id=run_id,
    )


def _start(db: ScopedDB, task: Task, item: Item, run_id: str) -> None:
    store.set_item(
        db,
        task.task_id,
        item.item_id,
        status="running",
        why="run_started",
        attempts=item.attempts + 1,
        last_run_id=run_id,
        run_id=run_id,
    )


def _exhausted(db: ScopedDB, task: Task, items: list[Item]) -> list[Item]:
    """Items at their run limit are held for a person; the rest may run."""
    ready: list[Item] = []
    for it in items:
        if it.attempts >= task.max_runs_per_item:
            store.set_item(
                db, task.task_id, it.item_id, status="needs_review", why="runs_exhausted"
            )
        else:
            ready.append(it)
    return ready


def launch(db: ScopedDB, settings: Settings, task: Task, items: list[Item]) -> ReadOutcome:
    """Fan out: one queued run per open read item; the worker merges it."""
    out = ReadOutcome()
    for it in _exhausted(db, task, items):
        run_id = run_id_for(db, task, it.item_id, it.attempts + 1)
        _start(db, task, it, run_id)
        with db.transaction() as cur:
            event_log.append(
                cur,
                tenant_id=db.tenant.tenant_id,
                topic=TOPIC,
                partition_key=f"{db.tenant.tenant_id}:{task.task_id}:{it.item_id}",
                source_ts=datetime.now(UTC),
                ref=json.dumps(
                    {
                        "task_id": task.task_id,
                        "item_id": it.item_id,
                        "run_id": run_id,
                        "bundle_id": task.bundle_id,
                    }
                ),
            )
        out.ran += 1
        out.changed += 1
        out.events.append(f"item_launched:{it.item_id}")
    return out


def single(
    db: ScopedDB,
    settings: Settings,
    task: Task,
    items: list[Item],
    providers: Providers,
    limit: int,
) -> ReadOutcome:
    """The single-run mode: one run over up to `limit` items, merged by item id."""
    out = ReadOutcome()
    ready = _exhausted(db, task, items)[:limit]
    if not ready:
        return out
    current = store.get(db, task.task_id)
    assert current is not None
    run_id = run_id_for(db, current, f"single-{current.passes + 1}", 1)
    for it in ready:
        _start(db, current, it, run_id)
    ready = [store.item(db, task.task_id, i.item_id) or i for i in ready]
    try:
        result = run_items(db, settings, current, ready, providers, run_id=run_id)
    except (RunStopped, BudgetExceeded) as exc:
        for it in ready:
            _hold(db, current, it, run_id, type(exc).__name__)
        out.ran, out.changed = 1, len(ready)
        out.events.append(f"single_run_held:{type(exc).__name__}")
        return out
    out.ran, out.usd = 1, result.usd
    out.changed = len(ready)
    out.events.extend(f"item_{status}:{item_id}" for item_id, status in result.merged.items())
    out.events.extend(f"merge_rejected:{r}" for r in result.rejections)
    return out


def reader(settings: Settings, providers: Providers | None = None) -> Reader:
    """The Reader the task runner hands its open reads to: fan-out when the kind may,
    else one single run; without a model key nothing runs and the items stay open."""
    config = load_kinds()
    cache: dict[str, Providers] = {}

    def read(db: ScopedDB, settings: Settings, task: Task, items: list[Item]) -> ReadOutcome:
        if fanout_enabled(settings, task.kind, config):
            return launch(db, settings, task, items)
        if "p" not in cache:
            cache["p"] = providers or Providers.from_settings(settings)
        if cache["p"].llm is None:
            return ReadOutcome(events=["reads_parked:no_model_key"])
        return single(db, settings, task, items, cache["p"], config.single_run_items)

    return read


class TaskItemJobs(ParallelConsumer):
    """Runs one fanned-out item per job (A-151): parks without a key or past the tenant's
    day; a failed run holds its item alone; a retried job resumes the item's own run."""

    name = "task_items"
    topic = TOPIC
    one_per_key = True

    def __init__(
        self, db: ScopedDB, settings: Settings, providers: Providers | None = None
    ) -> None:
        super().__init__(
            db,
            max_attempts=settings.ccp_max_attempts,
            visibility_seconds=settings.ccp_claim_visibility_seconds,
            base_delay=settings.ccp_retry_base_seconds,
        )
        self.settings = settings
        self._providers = providers
        self.merged: list[tuple[str, str]] = []

    @property
    def providers(self) -> Providers:
        if self._providers is None:
            self._providers = Providers.from_settings(self.settings)
        return self._providers

    def handle(self, record: LogRecord) -> None:
        ref = json.loads(record.ref)
        task = store.get(self._db, str(ref["task_id"]))
        it = store.item(self._db, str(ref["task_id"]), str(ref["item_id"]))
        if task is None or it is None:
            raise TerminalError(f"no task item {ref!r}")
        if it.status != "running" or it.last_run_id != ref["run_id"]:
            return  # superseded: a person settled it, or a later attempt owns it
        if task.status != "open":  # a parked task's run waits; a cancelled one's is dropped
            status: store.ItemStatus = "cancelled" if task.status == "cancelled" else "open"
            store.set_item(
                self._db, task.task_id, it.item_id, status=status, why=f"task_{task.status}"
            )
            return
        if self.providers.llm is None:
            raise ParkedError("ANTHROPIC_API_KEY is not set; task reads are unavailable")
        try:
            result = run_items(
                self._db, self.settings, task, [it], self.providers, run_id=str(ref["run_id"])
            )
        except (ParkedError, RetryableError):
            raise
        except Exception as exc:  # a failed item is held alone; the task goes on
            _hold(self._db, task, it, str(ref["run_id"]), f"run_failed:{type(exc).__name__}")
            self.merged.append((it.item_id, "needs_review"))
            return
        self.merged.extend(result.merged.items())


register("task_items")(lambda db, settings: TaskItemJobs(db, settings))


def drain(
    db: ScopedDB, settings: Settings, providers: Providers | None = None
) -> list[tuple[str, str]]:
    """Run every queued item job of this tenant (tests and `ccp tasks advance --drain`)."""
    jobs_ = TaskItemJobs(db, settings, providers)
    while jobs_.run_once():
        pass
    return jobs_.merged


def measure(
    db: ScopedDB,
    settings: Settings,
    kind: str,
    questions: list[dict[str, Any]],
    *,
    mode: str,
    providers: Providers,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Plan a labelled task, run its reads in one mode, and score each reading against its
    label; the single mode's record is the baseline a kind needs before it fans out."""
    if kind != "questionnaire":
        raise ValueError("measured fixtures exist for questionnaires only")
    moment = now or datetime.now(UTC)
    labels = {f"q:{q['id']}:read": str(q["expected"]) for q in questions if "expected" in q}
    task = runner.plan(
        db,
        settings,
        kind,
        f"measure {kind} {mode}",
        {"questions": questions},
        created_by="measure",
        now=moment,
    )
    config = load_kinds()

    def read(db_: ScopedDB, settings_: Settings, task_: Task, items: list[Item]) -> ReadOutcome:
        if mode == "fanout":
            return launch(db_, settings_, task_, items)
        return single(
            db_, settings_, task_, items, providers, max(len(items), config.single_run_items)
        )

    runner.advance(db, settings, task.task_id, reader=read, now=moment)
    if mode == "fanout":
        drain(db, settings, providers)
    items = {i.item_id: i for i in store.items_of(db, task.task_id)}
    scored = {k: (items[k].outcome or "") == v for k, v in labels.items() if k in items}
    spent = store.get(db, task.task_id)
    record = {
        "kind": kind,
        "mode": mode,
        "task_id": task.task_id,
        "bundle_id": release.bundle_id(settings),
        "items": len(scored),
        "correct": sum(scored.values()),
        "accuracy": round(sum(scored.values()) / len(scored), 4) if scored else 0.0,
        "usd": round(spent.spent_usd if spent else 0.0, 6),
        "statuses": dict(sorted(Counter(i.status for i in items.values()).items())),
        "measured_at": moment.isoformat(),
    }
    runs = settings.ccp_eval_runs_dir
    runs.mkdir(parents=True, exist_ok=True)
    stamp = moment.strftime("%Y%m%dT%H%M%S")
    (runs / f"{MEASURE_PREFIX}-{kind}-{mode}-{stamp}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )
    return record


jobs.JOBS["tasks"] = lambda db, settings, slot: runner.advance_open(
    db, settings, slot, reader=reader(settings)
)
