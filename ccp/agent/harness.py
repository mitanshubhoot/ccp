"""Harness metrics (spec.md A-155, report 6.8 "Harness metrics"): what each agent run did,
per graph and bundle, from its run log and its guardrail events.

    record(db, thread, events)   one run_metrics row for a finished run: its stops by reason,
                                 tool calls, errors and repeats per tool, its correction round
                                 and whether it worked, whether every code check passed the
                                 first time, what it swapped or dropped to stay under its
                                 ceiling, tokens and dollars from its recorded calls, and
                                 whether its result was accepted (no stop, no failed round)
    report(db)                   the rates per graph and bundle: stops by reason, tool-error
                                 and repeated-call rates per tool, correction rounds and their
                                 success, the first-pass share, tokens per accepted result
    fanout(db)                   per task kind: items per task, runs, failed items, merge
                                 rejections, items held after a correction round
    gate(db, baseline, cand.)    a bundle that raises the share of runs ending in a loop or
                                 cap stop does not ship

Nothing here is a model's opinion: every number is counted from what the runs recorded.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ccp.agent import core
from ccp.storage.scoped import ScopedDB

LOOP_STOPS = frozenset({"loop_repeat_call", "loop_repeat_result"})
CAP_STOPS = frozenset({"call_cap", "context_ceiling", "budget_exceeded"})
MIN_GATE_RUNS = 5  # fewer candidate runs than this cannot show a rise


@dataclass
class RunSummary:
    stops: list[str] = field(default_factory=list)
    model_calls: int = 0
    tool_calls: dict[str, dict[str, int]] = field(default_factory=dict)
    corrections: int = 0
    correction_succeeded: bool | None = None
    first_pass: bool = True
    context_swaps: int = 0
    context_dropped: int = 0
    tokens: int = 0
    usd: float = 0.0
    accepted: bool = True


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def stops_in(events: list[str]) -> list[str]:
    """Every way the run ended early: a tool loop's stop reason, or a structured call that
    its ceiling refused (`context_ceiling:<node>`)."""
    out: list[str] = []
    for e in events:
        parts = e.split(":")
        if parts[0] == "tool_loop_stopped" and len(parts) >= 3:
            out.append(parts[-1])
        elif parts[0] == "context_ceiling" and len(parts) == 2:
            out.append("context_ceiling")
    return out


def summarize(events: list[str], steps: list[tuple[str, Any, Any, Any]]) -> RunSummary:
    """steps: (kind, request, response, usage) rows of the run log, in order."""
    s = RunSummary(stops=stops_in(events))
    for kind, request, response, usage in steps:
        if kind == "model":
            s.model_calls += 1
            u = _json(usage) or {}
            s.tokens += sum(
                int(u.get(k) or 0)
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            )
            s.usd += float(u.get("usd") or 0.0)
        elif kind == "tool":
            name = str((_json(request) or {}).get("name", "?"))
            entry = s.tool_calls.setdefault(name, {"calls": 0, "errors": 0, "repeated": 0})
            entry["calls"] += 1
            entry["errors"] += int(bool((_json(response) or {}).get("is_error")))
    for e in events:
        if e.startswith("tool_call_repeated:"):
            name = e.split(":", 1)[1]
            entry = s.tool_calls.setdefault(name, {"calls": 0, "errors": 0, "repeated": 0})
            entry["calls"] += 1
            entry["repeated"] += 1
        elif e.startswith("correction_round:"):
            s.corrections += 1
        elif e.startswith("context_swapped:"):
            s.context_swaps += 1
        elif e.startswith("context_dropped:"):
            s.context_dropped += int(e.rsplit(":", 1)[1]) if e.rsplit(":", 1)[1].isdigit() else 1
    if "correction_succeeded" in events:
        s.correction_succeeded = True
    if any(e.startswith("correction_failed:") for e in events):
        s.correction_succeeded = False
    s.first_pass = s.corrections == 0 and not core.failures_in(events)
    s.accepted = not s.stops and s.correction_succeeded is not False
    return s


def record(db: ScopedDB, thread_id: str, events: list[str] | tuple[str, ...]) -> RunSummary:
    """One row for a finished run, from its thread, its run log and its events."""
    thread = db.fetch_one(
        "SELECT graph, bundle_id FROM agent_threads WHERE thread_id = %s", (thread_id,)
    )
    graph, bundle_id = (str(thread[0]), str(thread[1] or "unknown")) if thread else ("?", "?")
    steps = db.fetch_all(
        "SELECT kind, request, response, usage FROM agent_steps WHERE thread_id = %s ORDER BY seq",
        (thread_id,),
    )
    s = summarize(list(events), [(r[0], r[1], r[2], r[3]) for r in steps])
    db.execute(
        "INSERT INTO run_metrics (tenant_id, thread_id, graph, bundle_id, stops, model_calls,"
        " tool_calls, corrections, correction_succeeded, first_pass, context_swaps,"
        " context_dropped, tokens, usd, accepted)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            thread_id,
            graph,
            bundle_id,
            json.dumps(s.stops),
            s.model_calls,
            json.dumps(s.tool_calls, sort_keys=True),
            s.corrections,
            s.correction_succeeded,
            s.first_pass,
            s.context_swaps,
            s.context_dropped,
            s.tokens,
            Decimal(str(round(s.usd, 6))),
            s.accepted,
        ),
    )
    return s


def _rows(
    db: ScopedDB, graph: str | None, bundle_id: str | None, since: datetime | None
) -> list[tuple[Any, ...]]:
    return db.fetch_all(
        "SELECT graph, bundle_id, stops, tool_calls, corrections, correction_succeeded,"
        " first_pass, context_swaps, context_dropped, tokens, usd, accepted FROM run_metrics"
        " WHERE (%s::text IS NULL OR graph = %s) AND (%s::text IS NULL OR bundle_id = %s)"
        " AND (%s::timestamptz IS NULL OR recorded_at >= %s)",
        (graph, graph, bundle_id, bundle_id, since, since),
    )


def report(
    db: ScopedDB,
    graph: str | None = None,
    bundle_id: str | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for row in _rows(db, graph, bundle_id, since):
        groups.setdefault((str(row[0]), str(row[1])), []).append(row)
    out: list[dict[str, Any]] = []
    for (g, b), rows in sorted(groups.items()):
        runs = len(rows)
        stops: Counter[str] = Counter()
        tools: dict[str, Counter[str]] = {}
        for row in rows:
            stops.update(set(_json(row[2]) or []))
            for name, counts in (_json(row[3]) or {}).items():
                tools.setdefault(name, Counter()).update(counts)
        rounds = [r for r in rows if r[4]]
        accepted = [r for r in rows if r[11]]
        out.append(
            {
                "graph": g,
                "bundle_id": b,
                "runs": runs,
                "stop_rates": {k: round(v / runs, 4) for k, v in sorted(stops.items())},
                "loop_or_cap_stop_rate": round(_stopped(rows) / runs, 4),
                "tools": {
                    name: {
                        "calls": c["calls"],
                        "error_rate": round(c["errors"] / c["calls"], 4) if c["calls"] else 0.0,
                        "repeat_rate": round(c["repeated"] / c["calls"], 4) if c["calls"] else 0.0,
                    }
                    for name, c in sorted(tools.items())
                },
                "correction_rounds": len(rounds),
                "correction_success_rate": (
                    round(sum(1 for r in rounds if r[5]) / len(rounds), 4) if rounds else None
                ),
                "first_pass_rate": round(sum(1 for r in rows if r[6]) / runs, 4),
                "context_swaps": sum(int(r[7]) for r in rows),
                "context_dropped": sum(int(r[8]) for r in rows),
                "accepted": len(accepted),
                "tokens_per_accepted": (
                    round(sum(int(r[9]) for r in rows) / len(accepted)) if accepted else None
                ),
                "usd_per_accepted": (
                    round(sum(float(r[10]) for r in rows) / len(accepted), 6) if accepted else None
                ),
            }
        )
    return out


def _stopped(rows: list[tuple[Any, ...]]) -> int:
    return sum(1 for r in rows if set(_json(r[2]) or []) & (LOOP_STOPS | CAP_STOPS))


def fanout(db: ScopedDB, since: datetime | None = None) -> list[dict[str, Any]]:
    """Per task kind: how the fanned-out reads went (A-151, A-152)."""
    rows = db.fetch_all(
        "SELECT t.kind, e.task_id, e.item_id, e.kind, e.detail FROM task_events e"
        " JOIN tasks t ON t.task_id = e.task_id"
        " WHERE (%s::timestamptz IS NULL OR e.recorded_at >= %s)",
        (since, since),
    )
    by_kind: dict[str, dict[str, Any]] = {}
    for kind, task_id, item_id, event_kind, detail in rows:
        k = by_kind.setdefault(
            str(kind),
            {
                "tasks": set(),
                "runs": 0,
                "items_run": set(),
                "rejections": 0,
                "held": 0,
                "held_after_correction": 0,
            },
        )
        d = _json(detail) or {}
        k["tasks"].add(task_id)
        if event_kind == "run_finished":
            k["runs"] += 1
            k["items_run"].add((task_id, item_id))
            k["rejections"] += int(d.get("rejections") or 0)
            k["held_after_correction"] += int(bool(d.get("failed_after_correction")))
        elif event_kind == "item_status" and d.get("status") == "needs_review":
            k["held"] += 1
    return [
        {
            "kind": kind,
            "tasks": len(k["tasks"]),
            "runs": k["runs"],
            "items_per_task": round(len(k["items_run"]) / len(k["tasks"]), 2) if k["tasks"] else 0,
            "failed_items": k["held"],
            "merge_rejections": k["rejections"],
            "held_after_correction": k["held_after_correction"],
        }
        for kind, k in sorted(by_kind.items())
    ]


@dataclass(frozen=True)
class GateResult:
    graph: str
    baseline_runs: int
    candidate_runs: int
    baseline_rate: float | None
    candidate_rate: float | None
    verdict: str  # pass | blocked | insufficient

    @property
    def blocks(self) -> bool:
        return self.verdict == "blocked"


def gate(db: ScopedDB, baseline: str, candidate: str, graph: str | None = None) -> list[GateResult]:
    """Per graph: the candidate bundle may not end a larger share of its runs in a loop or
    cap stop than the baseline did. Too few candidate runs is `insufficient`, never a pass
    that hides a rise; a graph the baseline never ran has nothing to rise from."""
    out: list[GateResult] = []
    graphs = sorted({str(r[0]) for r in _rows(db, graph, candidate, None)})
    for g in graphs:
        base = _rows(db, g, baseline, None)
        cand = _rows(db, g, candidate, None)
        base_rate = _stopped(base) / len(base) if base else None
        cand_rate = _stopped(cand) / len(cand) if cand else None
        if len(cand) < MIN_GATE_RUNS:
            verdict = "insufficient"
        elif base_rate is None or cand_rate is None or cand_rate <= base_rate:
            verdict = "pass"
        else:
            verdict = "blocked"
        out.append(
            GateResult(
                g,
                len(base),
                len(cand),
                round(base_rate, 4) if base_rate is not None else None,
                round(cand_rate, 4) if cand_rate is not None else None,
                verdict,
            )
        )
    return out
