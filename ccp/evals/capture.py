"""``ccp evals capture <trace_id>``: a golden case from any eval trace (spec.md Layer 11).

The runner wraps every target call in a step named ``eval.<target>`` whose inputs are the
case input and whose output is the target's observed output. Capturing that step id (or the
run id it belongs to) writes an exact-graded case into ``evals/golden/captured.yaml`` with
the observed output as its expectation, and records in the notes whatever replay material
the trace carries: chunk ids, the fence nonce, prompt and schema versions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ccp.evals.schema import GoldenCase
from ccp.observability import steps

CAPTURED_FILE = "captured.yaml"
VOLATILE_KEYS = frozenset(
    {
        "latency_ms",
        "thread_id",
        "verdict_id",
        "run_id",
        "text",
        "reasoning",
        "markdown",
        "summary",
        "root_cause",
        "created_at",
    }
)  # model prose flaps between runs; captured cases pin structure, not wording
REPLAY_KEYS = (
    "retrieved_chunk_ids",
    "chunk_ids",
    "fence_nonce",
    "prompt_version",
    "schema_version",
    "model",
)


def find_step(log_path: Path, trace_id: str) -> dict[str, Any] | None:
    """The eval step with this step id, or the last eval step of this run id."""
    if not log_path.exists():
        return None
    by_run: dict[str, Any] | None = None
    with log_path.open(encoding="utf-8") as fh:
        for raw in fh:
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            name = str(line.get("gen_ai.operation.name") or "")
            if not name.startswith("eval."):
                continue
            if line.get("ccp.step_id") == trace_id:
                return steps.resolve_blobs(line, log_path)
            if line.get("ccp.run_id") == trace_id:
                by_run = dict(line)
    return steps.resolve_blobs(by_run, log_path) if by_run is not None else None


def _collect(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _collect(value, key)
            if found is not None:
                return found
    return None


def expectation_from(output: dict[str, Any]) -> dict[str, Any]:
    """The observed output as an exact expectation, minus volatile fields; a confidence
    becomes a lower bound so a re-run that is marginally more confident still passes."""
    expected: dict[str, Any] = {}
    for key, value in output.items():
        if key in VOLATILE_KEYS:
            continue
        if key == "confidence" and isinstance(value, int | float):
            expected[key] = {"$gte": round(max(0.0, float(value) - 0.1), 3)}
        else:
            expected[key] = value
    return {"$subset": expected} if expected else {}


def case_from_step(line: dict[str, Any], *, now: datetime | None = None) -> GoldenCase:
    name = str(line.get("gen_ai.operation.name") or "")
    target = name.removeprefix("eval.")
    inputs = dict(line.get("inputs") or {})
    output = dict(line.get("output") or {})
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    replay = {
        key: _collect({"inputs": inputs, "output": output}, key)
        for key in REPLAY_KEYS
        if _collect({"inputs": inputs, "output": output}, key) is not None
    }
    notes = (
        f"captured from trace {line.get('ccp.step_id')} (run {line.get('ccp.run_id')})"
        f" on {stamp}; replay material: {json.dumps(replay, default=str)}"
    )
    return GoldenCase(
        id=f"cap-{target}-{str(line.get('ccp.step_id') or stamp).lower()}",
        level="component",
        difficulty="edge",
        source="synthetic",
        tenant=inputs.get("tenant"),
        target=target,
        input=dict(inputs.get("input") or {}),
        expected=expectation_from(output),
        notes=notes,
        tags=["captured", target],
        requires=list(inputs.get("requires") or []),
        origin="customer",  # a trace may hold tenant data: never a cross-family judge (A-103)
    )


def append_case(case: GoldenCase, golden_dir: Path, filename: str = CAPTURED_FILE) -> Path:
    golden_dir.mkdir(parents=True, exist_ok=True)
    path = golden_dir / filename
    existing: list[dict[str, Any]] = []
    if path.exists():
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if any(item.get("id") == case.id for item in existing):
        return path
    existing.append(case.model_dump(mode="json", exclude_none=True))
    header = "# Golden cases captured from traces with `ccp evals capture <trace_id>`.\n"
    path.write_text(
        header + yaml.safe_dump(existing, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return path


def capture(
    log_path: Path,
    trace_id: str,
    golden_dir: Path,
    *,
    filename: str = CAPTURED_FILE,
    now: datetime | None = None,
) -> tuple[GoldenCase, Path]:
    line = find_step(log_path, trace_id)
    if line is None:
        raise LookupError(f"no eval step with trace id {trace_id!r} in {log_path}")
    case = case_from_step(line, now=now)
    return case, append_case(case, golden_dir, filename)
