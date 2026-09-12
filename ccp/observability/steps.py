"""The step logger: one JSON line per step (spec.md Layer 12).

Phase 0 stub: writes ``{CCP_DATA_DIR}/logs/steps.jsonl`` with OpenTelemetry GenAI
attribute names where a convention exists (``gen_ai.*``) and ``ccp.*`` otherwise.
Langfuse emission, blob offload, and cost arrive in Phase 16; the schema is fixed now so
every earlier phase logs through the same call.

    with step("tier2.extract", layer="evaluators", tenant=tenant, inputs={...}) as s:
        ...
        s.model("claude-haiku-4-5", prompt_version="gh4_extract_v1")
        s.usage(input_tokens=..., output_tokens=..., cache_read=..., cache_write=...)
        s.output({...})
"""

from __future__ import annotations

import json
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
from ccp.tenancy import TenantContext

_log_path: Path = Path("data") / "logs" / "steps.jsonl"
_run_id: ContextVar[str | None] = ContextVar("ccp_run_id", default=None)
_parent_step: ContextVar[str | None] = ContextVar("ccp_parent_step", default=None)


def configure(settings: Settings) -> None:
    global _log_path
    _log_path = settings.logs_dir / "steps.jsonl"


def new_run() -> str:
    """Start a new run id for the current context; steps nest under it."""
    run_id = str(ULID())
    _run_id.set(run_id)
    return run_id


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
    usage_fields: dict[str, int] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    error: str | None = None

    def set_output(self, value: Mapping[str, Any]) -> None:
        self.output = dict(value)

    def model(self, model_id: str, *, prompt_version: str | None = None) -> None:
        self.model_id = model_id
        self.prompt_version = prompt_version

    def usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> None:
        self.usage_fields = {
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
            "gen_ai.usage.cache_read_input_tokens": cache_read,
            "gen_ai.usage.cache_creation_input_tokens": cache_write,
        }

    def event(self, name: str) -> None:
        """Record a guardrail or degrade event, e.g. 'injection_marker_detected:chunk_42'."""
        self.events.append(name)


@contextmanager
def step(
    name: str,
    *,
    layer: str,
    tenant: TenantContext | None = None,
    inputs: Mapping[str, Any] | None = None,
) -> Iterator[StepRecord]:
    record = StepRecord(
        step_id=str(ULID()),
        name=name,
        layer=layer,
        tenant_id=tenant.tenant_id if tenant else None,
        inputs=dict(inputs or {}),
    )
    parent = _parent_step.get()
    token = _parent_step.set(record.step_id)
    started = datetime.now(UTC)
    t0 = time.perf_counter()
    try:
        yield record
    except BaseException as exc:
        record.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _parent_step.reset(token)
        latency_ms = round((time.perf_counter() - t0) * 1000, 3)
        line: dict[str, Any] = {
            "ccp.step_id": record.step_id,
            "ccp.parent_step_id": parent,
            "ccp.run_id": _run_id.get(),
            "ccp.tenant_id": record.tenant_id,
            "ccp.layer": record.layer,
            "gen_ai.operation.name": record.name,
            "started_at": started.isoformat(),
            "latency_ms": latency_ms,
            "inputs": record.inputs,
            "output": record.output,
            "gen_ai.request.model": record.model_id,
            "ccp.prompt_version": record.prompt_version,
            **record.usage_fields,
            "ccp.guardrail_events": record.events,
            "error": record.error,
        }
        _append(line)


def _append(line: Mapping[str, Any]) -> None:
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    with _log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, default=str, ensure_ascii=False) + "\n")
