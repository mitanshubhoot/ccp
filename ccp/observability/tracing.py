"""Langfuse Cloud emission behind a sink seam (spec.md Layer 12).

The JSONL step log is the source of truth and Langfuse is the view: every step becomes one
observation (a generation when a model was recorded, a span otherwise), nested under its
parent step and grouped into one trace per run id. When the keys are absent nothing is
emitted; when Langfuse is unreachable at startup the logger prints one warning and
continues; when the SDK fails mid-run the sink is disabled after one warning. Nothing here
can block or drop a step.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ccp import credentials
from ccp.config import Settings

Usage = dict[str, int]
MaskFn = Callable[..., Any]  # called by the SDK as mask(data=...)
_mask: MaskFn | None = None


def register_mask(fn: MaskFn | None) -> None:
    """The function every Langfuse payload passes through before it leaves the machine
    (the secrets patterns register theirs at import; observability cannot import them
    itself because of the layer order)."""
    global _mask
    _mask = fn


def registered_mask() -> MaskFn | None:
    return _mask


class Sink(Protocol):
    def start(
        self,
        *,
        step_id: str,
        name: str,
        kind: str,
        trace_key: str | None,
        parent_key: str | None,
        inputs: Any,
        metadata: dict[str, Any],
    ) -> Any: ...

    def end(
        self,
        handle: Any,
        *,
        output: Any,
        metadata: dict[str, Any],
        model: str | None,
        usage: Usage,
        cost_usd: float | None,
        error: str | None,
    ) -> None: ...

    def flush(self) -> None: ...


def trace_key_for(run_id: str | None) -> str | None:
    """A W3C-shaped trace id (32 hex) derived from the run id, so a run is one trace."""
    if run_id is None:
        return None
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]


@dataclass
class RecordingSink:
    """Test double: records what would have been emitted, with the nesting."""

    spans: list[dict[str, Any]] = field(default_factory=list)
    flushed: int = 0

    def start(
        self,
        *,
        step_id: str,
        name: str,
        kind: str,
        trace_key: str | None,
        parent_key: str | None,
        inputs: Any,
        metadata: dict[str, Any],
    ) -> Any:
        self.spans.append(
            {
                "step_id": step_id,
                "name": name,
                "kind": kind,
                "trace_key": trace_key,
                "parent_key": parent_key,
                "inputs": inputs,
                "metadata": metadata,
                "ended": False,
            }
        )
        return len(self.spans) - 1

    def end(
        self,
        handle: Any,
        *,
        output: Any,
        metadata: dict[str, Any],
        model: str | None,
        usage: Usage,
        cost_usd: float | None,
        error: str | None,
    ) -> None:
        self.spans[int(handle)].update(
            {
                "ended": True,
                "output": output,
                "metadata": metadata,
                "model": model,
                "usage": usage,
                "cost_usd": cost_usd,
                "error": error,
            }
        )

    def flush(self) -> None:
        self.flushed += 1


class FailingSink:
    """Test double: raises on first use, which must disable emission without losing a step."""

    def start(self, **kwargs: Any) -> Any:
        raise RuntimeError("sink exploded")

    def end(self, handle: Any, **kwargs: Any) -> None:
        raise RuntimeError("sink exploded")

    def flush(self) -> None:
        return None


class LangfuseSink:
    """The real thing: one Langfuse observation per step via the v3 SDK."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._ids: dict[str, str] = {}  # step id -> observation id, while the step is open

    def start(
        self,
        *,
        step_id: str,
        name: str,
        kind: str,
        trace_key: str | None,
        parent_key: str | None,
        inputs: Any,
        metadata: dict[str, Any],
    ) -> Any:
        parent = self._ids.get(parent_key) if parent_key else None
        # The v3/v4 SDK links an observation to a trace and a parent through trace_context
        # (W3C ids); there are no trace_id / parent_observation_id keywords.
        trace_context: dict[str, str] = {}
        if trace_key:
            trace_context["trace_id"] = trace_key
        if parent:
            trace_context["parent_span_id"] = parent
        observation = self._client.start_observation(
            name=name,
            as_type=kind,
            input=inputs,
            metadata=metadata,
            trace_context=trace_context or None,
        )
        self._ids[step_id] = observation.id
        return (step_id, observation)

    def end(
        self,
        handle: Any,
        *,
        output: Any,
        metadata: dict[str, Any],
        model: str | None,
        usage: Usage,
        cost_usd: float | None,
        error: str | None,
    ) -> None:
        step_id, observation = handle
        fields: dict[str, Any] = {
            "output": output,
            "metadata": metadata,
            "level": "ERROR" if error else "DEFAULT",
            "status_message": error,
        }
        if model:
            fields["model"] = model
        if usage:
            fields["usage_details"] = {
                "input": usage.get("input_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read", 0),
                "cache_creation_input_tokens": usage.get("cache_write", 0),
            }
        if cost_usd is not None:
            fields["cost_details"] = {"total": cost_usd}
        try:
            observation.update(**fields)
        finally:
            observation.end()
            self._ids.pop(step_id, None)

    def flush(self) -> None:
        self._client.flush()


def default_probe(host: str) -> None:
    """Raise when Langfuse is unreachable; a blocking check made once at startup only."""
    httpx.get(f"{host.rstrip('/')}/api/public/health", timeout=3.0).raise_for_status()


def build_sink(
    settings: Settings, *, probe: Callable[[str], None] | None = None
) -> tuple[Sink | None, str | None]:
    """(sink, warning): no keys -> (None, None); unreachable or SDK failure -> (None, why)."""
    public = credentials.secret(settings, "trace:langfuse_public")
    secret = credentials.secret(settings, "trace:langfuse_secret")
    if public is None or secret is None:
        return None, None
    if not public.strip() or not secret.strip():
        return None, None  # an empty variable (an unset CI secret) is not a key
    host = settings.langfuse_host
    try:
        (probe or default_probe)(host)
    except Exception as exc:  # noqa: BLE001 - any failure means "not reachable now"
        return None, (
            f"Langfuse at {host} is unreachable ({type(exc).__name__}: {exc}); tracing is off"
            " for this process and steps.jsonl remains the source of truth"
        )
    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=public,
            secret_key=secret,
            host=host,
            flush_interval=2.0,
            mask=_mask,
        )
    except Exception as exc:  # noqa: BLE001 - the SDK must never take the run down
        return (
            None,
            f"Langfuse client could not start ({type(exc).__name__}: {exc}); tracing is off",
        )
    return LangfuseSink(client), None
