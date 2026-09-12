"""The step logger: one JSON line per step (spec.md Layer 12).

Writes ``{CCP_DATA_DIR}/logs/steps.jsonl`` with OpenTelemetry GenAI attribute names where a
convention exists (``gen_ai.*``) and ``ccp.*`` otherwise, offloads inputs and outputs over
4 KB to ``logs/blobs/{step_id}.{inputs|output}.json``, multiplies usage by the price table
into ``ccp.cost_usd``, and emits the same record to Langfuse as an observation nested under
its parent step (``ccp/observability/tracing.py``). The JSONL file is the source of truth;
Langfuse is the view and can be down without a step being lost.

    with step("tier2.extract", layer="evaluators", tenant=tenant, inputs={...}) as s:
        ...
        s.model("claude-haiku-4-5", prompt_version="gh4_extract_v1", prompt_sha256="...")
        s.usage(input_tokens=..., output_tokens=..., cache_read=..., cache_write=...)
        s.set_output({...})
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ulid import ULID

from ccp.config import Settings
from ccp.observability import cost, tracing
from ccp.observability.tracing import Sink
from ccp.tenancy import TenantContext

BLOB_LIMIT_BYTES = 4096

_log_path: Path = Path("data") / "logs" / "steps.jsonl"
_run_id: ContextVar[str | None] = ContextVar("ccp_run_id", default=None)
_parent_step: ContextVar[str | None] = ContextVar("ccp_parent_step", default=None)
_current: ContextVar[StepRecord | None] = ContextVar("ccp_current_step", default=None)
_sink: Sink | None = None
_startup_warning: str | None = None


def configure(
    settings: Settings, *, sink: Sink | str | None = "auto", probe: Any = None
) -> str | None:
    """Point the log at the settings' data directory and set up tracing. Returns the startup
    warning when Langfuse keys are set but the service is unreachable (printed once)."""
    global _log_path, _sink, _startup_warning
    _log_path = settings.logs_dir / "steps.jsonl"
    _startup_warning = None
    if sink == "auto":
        built, warning = tracing.build_sink(settings, probe=probe)
        _sink = built
        if warning:
            _startup_warning = warning
            print(f"warning: {warning}", file=sys.stderr)
    elif isinstance(sink, str):
        raise ValueError(f"unknown sink {sink!r}")
    else:
        _sink = sink
    return _startup_warning


def set_sink(sink: Sink | None) -> None:
    global _sink
    _sink = sink


def sink_active() -> bool:
    return _sink is not None


def log_path() -> Path:
    """Where step lines go right now (the eval runner reads usage back from it)."""
    return _log_path


def blobs_dir() -> Path:
    return _log_path.parent / "blobs"


def new_run() -> str:
    """Start a new run id for the current context; steps nest under it."""
    run_id = str(ULID())
    _run_id.set(run_id)
    return run_id


def current_run_id() -> str | None:
    return _run_id.get()


def _disable_sink(exc: BaseException) -> None:
    global _sink
    _sink = None
    print(
        f"warning: tracing sink failed ({type(exc).__name__}: {exc}); tracing is off for this"
        " process and steps.jsonl remains the source of truth",
        file=sys.stderr,
    )


@dataclass
class StepRecord:
    step_id: str
    name: str
    layer: str
    tenant_id: str | None
    inputs: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    usage_fields: dict[str, int] = field(default_factory=dict)
    usage_raw: dict[str, int] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    error: str | None = None
    handle: Any = None

    def set_output(self, value: Mapping[str, Any]) -> None:
        self.output = dict(value)

    def model(
        self, model_id: str, *, prompt_version: str | None = None, prompt_sha256: str | None = None
    ) -> None:
        self.model_id = model_id
        self.prompt_version = prompt_version
        self.prompt_sha256 = prompt_sha256

    def usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> None:
        self.usage_raw = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read": cache_read,
            "cache_write": cache_write,
        }
        self.usage_fields = {
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
            "gen_ai.usage.cache_read_input_tokens": cache_read,
            "gen_ai.usage.cache_creation_input_tokens": cache_write,
        }

    def add_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> None:
        """Accumulate usage (a node step may make several model calls)."""
        self.usage(
            input_tokens=self.usage_raw.get("input_tokens", 0) + input_tokens,
            output_tokens=self.usage_raw.get("output_tokens", 0) + output_tokens,
            cache_read=self.usage_raw.get("cache_read", 0) + cache_read,
            cache_write=self.usage_raw.get("cache_write", 0) + cache_write,
        )

    def event(self, name: str) -> None:
        """Record a guardrail or degrade event, e.g. 'injection_marker_detected:chunk_42'."""
        self.events.append(name)

    def cost_usd(self) -> float | None:
        if not self.model_id or not self.usage_raw:
            return None
        return round(
            cost.estimate_usd(
                self.model_id,
                input_tokens=self.usage_raw.get("input_tokens", 0),
                output_tokens=self.usage_raw.get("output_tokens", 0),
                cache_read_tokens=self.usage_raw.get("cache_read", 0),
                cache_write_tokens=self.usage_raw.get("cache_write", 0),
            ),
            8,
        )


def note_model_call(
    model_id: str | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    prompt_version: str | None = None,
) -> None:
    """Attribute a model call to the current step, so cost accounting sees every call the
    agents and the judge make, not only tier 2's."""
    record = _current.get()
    if record is None or not model_id:
        return
    record.model_id = model_id
    if prompt_version:
        record.prompt_version = prompt_version
    record.add_usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
    )


def _offload(step_id: str, kind: str, value: Any) -> Any:
    """Inputs and outputs over the limit go to logs/blobs/, referenced by relative path."""
    if value is None:
        return None
    text = json.dumps(value, default=str, ensure_ascii=False)
    if len(text.encode("utf-8")) <= BLOB_LIMIT_BYTES:
        return value
    directory = blobs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{step_id}.{kind}.json"
    path.write_text(text, encoding="utf-8")
    return {"$blob": f"blobs/{path.name}", "bytes": len(text.encode("utf-8"))}


def resolve_blobs(line: Mapping[str, Any], log: Path | None = None) -> dict[str, Any]:
    """The line with any offloaded inputs or output read back in."""
    base = (log or _log_path).parent
    out = dict(line)
    for key in ("inputs", "output"):
        value = out.get(key)
        if isinstance(value, dict) and "$blob" in value:
            path = base / str(value["$blob"])
            out[key] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    return out


def read_lines(log: Path | None = None) -> Iterator[dict[str, Any]]:
    path = log or _log_path
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            try:
                yield resolve_blobs(json.loads(raw), path)
            except json.JSONDecodeError:
                continue


@contextmanager
def step(
    name: str,
    *,
    layer: str,
    tenant: TenantContext | None = None,
    inputs: Mapping[str, Any] | None = None,
    kind: str = "span",
) -> Iterator[StepRecord]:
    record = StepRecord(
        step_id=str(ULID()),
        name=name,
        layer=layer,
        tenant_id=tenant.tenant_id if tenant else None,
        inputs=dict(inputs or {}),
    )
    parent = _parent_step.get()
    run_id = _run_id.get()
    token = _parent_step.set(record.step_id)
    current_token = _current.set(record)
    started = datetime.now(UTC)
    t0 = time.perf_counter()
    if _sink is not None:
        try:
            record.handle = _sink.start(
                step_id=record.step_id,
                name=name,
                kind=kind,
                trace_key=tracing.trace_key_for(run_id),
                parent_key=parent,
                inputs=record.inputs,
                metadata={
                    "ccp.step_id": record.step_id,
                    "ccp.parent_step_id": parent,
                    "ccp.run_id": run_id,
                    "ccp.tenant_id": record.tenant_id,
                    "ccp.layer": layer,
                },
            )
        except Exception as exc:  # noqa: BLE001 - the view must never block the record
            _disable_sink(exc)
    try:
        yield record
    except BaseException as exc:
        record.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _parent_step.reset(token)
        _current.reset(current_token)
        latency_ms = round((time.perf_counter() - t0) * 1000, 3)
        usd = record.cost_usd()
        line: dict[str, Any] = {
            "ccp.step_id": record.step_id,
            "ccp.parent_step_id": parent,
            "ccp.run_id": run_id,
            "ccp.tenant_id": record.tenant_id,
            "ccp.layer": record.layer,
            "gen_ai.operation.name": record.name,
            "started_at": started.isoformat(),
            "latency_ms": latency_ms,
            "inputs": _offload(record.step_id, "inputs", record.inputs),
            "output": _offload(record.step_id, "output", record.output),
            "gen_ai.request.model": record.model_id,
            "ccp.prompt_version": record.prompt_version,
            "ccp.prompt_sha256": record.prompt_sha256,
            **record.usage_fields,
            "ccp.cost_usd": usd,
            "ccp.cost_basis": cost.COST_BASIS if usd is not None else None,
            "ccp.guardrail_events": record.events,
            "error": record.error,
        }
        _append(line)
        if _sink is not None and record.handle is not None:
            try:
                _sink.end(
                    record.handle,
                    output=record.output,
                    metadata={
                        "ccp.latency_ms": latency_ms,
                        "ccp.prompt_version": record.prompt_version,
                        "ccp.prompt_sha256": record.prompt_sha256,
                        "ccp.guardrail_events": record.events,
                        "ccp.cost_basis": cost.COST_BASIS if usd is not None else None,
                    },
                    model=record.model_id,
                    usage=record.usage_raw,
                    cost_usd=usd,
                    error=record.error,
                )
            except Exception as exc:  # noqa: BLE001
                _disable_sink(exc)


def flush() -> None:
    if _sink is not None:
        try:
            _sink.flush()
        except Exception as exc:  # noqa: BLE001
            _disable_sink(exc)


def _append(line: Mapping[str, Any]) -> None:
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    with _log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, default=str, ensure_ascii=False) + "\n")
