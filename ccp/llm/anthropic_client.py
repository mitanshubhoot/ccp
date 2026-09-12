"""The Anthropic implementation of LLMClient. The only module that imports the SDK.

    extract   messages.parse(..., output_format=schema)   structured output, one re-ask on a
              schema miss, then terminal
    complete  messages.create(...)                        plain text
    batches   submit_batch / collect_batch                offline work at half price, keyed by
                                                          custom id (index-time enrichment,
                                                          the eval suite)

Errors map onto the queue's taxonomy (ccp.llm.errors): 429, 529, 5xx, connection and
timeout retry with exponential backoff and jitter inside the client, then bubble up as
retryable; 400, 401, 403, 404 and refusals are terminal. The budget tracker is charged
after every response and raises past a cap.

Prompt layout (spec.md R16): `system` blocks are stable-first and the block flagged
`cache=True` carries an explicit `cache_control` breakpoint; per-call material lives in the
user turn. Note the model-specific minimum cacheable prefix (4096 tokens on Haiku 4.5):
a shorter prefix silently does not cache.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar, cast

import anthropic
from pydantic import BaseModel

from ccp.config import Settings
from ccp.llm.budget import BudgetTracker
from ccp.llm.client import Completion, Extraction, SystemBlock, Usage
from ccp.llm.errors import RetryableModelError, TerminalModelError, classify_status
from ccp.observability import cost

T = TypeVar("T", bound=BaseModel)

RETRY_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.0
REASK_SUFFIX = (
    "\n\nYour previous answer did not match the required schema. Answer again with only the "
    "fields of the schema, every required field present."
)


def system_param(system: list[SystemBlock]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for block in system:
        item: dict[str, Any] = {"type": "text", "text": block.text}
        if block.cache:
            item["cache_control"] = {"type": "ephemeral"}
        blocks.append(item)
    return blocks


def _usage(model: str, raw: Any, *, batch: bool = False) -> Usage:
    input_tokens = int(getattr(raw, "input_tokens", 0) or 0)
    output_tokens = int(getattr(raw, "output_tokens", 0) or 0)
    cache_read = int(getattr(raw, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(raw, "cache_creation_input_tokens", 0) or 0)
    usd = cost.estimate_usd(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        batch=batch,
    )
    return Usage(input_tokens, output_tokens, cache_read, cache_write, usd)


def _classify(exc: Exception) -> Exception:
    if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
        return RetryableModelError(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, anthropic.APIStatusError):
        return classify_status(exc.status_code, getattr(exc, "message", str(exc)))
    return TerminalModelError(f"{type(exc).__name__}: {exc}")


class AnthropicClient:
    provider = "anthropic"

    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if client is None:
            key = settings.anthropic_api_key
            if key is None:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            client = anthropic.Anthropic(api_key=key.get_secret_value(), max_retries=0)
        self._client = client
        self._sleep = sleep

    # -- retry ------------------------------------------------------------------------

    def _with_retry(self, call: Callable[[], Any]) -> Any:
        last: Exception | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return call()
            except Exception as exc:  # noqa: BLE001 - every SDK error is classified below
                mapped = _classify(exc)
                if not isinstance(mapped, RetryableModelError) or attempt == RETRY_ATTEMPTS - 1:
                    raise mapped from exc
                last = mapped
                self._sleep(RETRY_BASE_SECONDS * (2**attempt) + random.uniform(0, 0.5))  # noqa: S311
        assert last is not None
        raise last

    # -- online -----------------------------------------------------------------------

    def extract(
        self,
        *,
        schema: type[T],
        system: list[SystemBlock],
        user: str,
        model: str,
        tracker: BudgetTracker,
        max_tokens: int = 2048,
    ) -> Extraction:
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_param(system),
            "messages": [{"role": "user", "content": user}],
        }
        request = {**params, "output_format": schema.__name__}
        response = self._with_retry(
            lambda: self._client.messages.parse(**params, output_format=schema)
        )
        usage = _usage(model, response.usage)
        tracker.charge(
            input_tokens=usage.input_tokens + usage.cache_read_input_tokens,
            output_tokens=usage.output_tokens,
            usd=usage.usd,
        )
        if getattr(response, "stop_reason", None) == "refusal":
            raise TerminalModelError("model refused the extraction request")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:  # one re-ask, then terminal (spec.md D9)
            params["messages"] = [{"role": "user", "content": user + REASK_SUFFIX}]
            response = self._with_retry(
                lambda: self._client.messages.parse(**params, output_format=schema)
            )
            second = _usage(model, response.usage)
            tracker.charge(
                input_tokens=second.input_tokens + second.cache_read_input_tokens,
                output_tokens=second.output_tokens,
                usd=second.usd,
            )
            usage = Usage(
                usage.input_tokens + second.input_tokens,
                usage.output_tokens + second.output_tokens,
                usage.cache_read_input_tokens + second.cache_read_input_tokens,
                usage.cache_creation_input_tokens + second.cache_creation_input_tokens,
                usage.usd + second.usd,
            )
            parsed = getattr(response, "parsed_output", None)
            if parsed is None:
                raise TerminalModelError("schema refusal after one re-ask")
        return Extraction(
            parsed=parsed,
            usage=usage,
            model=str(getattr(response, "model", model)),
            request=request,
            response=_to_dict(response),
        )

    def complete(
        self,
        *,
        system: list[SystemBlock],
        user: str,
        model: str,
        tracker: BudgetTracker,
        max_tokens: int = 512,
    ) -> Completion:
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_param(system),
            "messages": [{"role": "user", "content": user}],
        }
        response = self._with_retry(lambda: self._client.messages.create(**params))
        usage = _usage(model, response.usage)
        tracker.charge(
            input_tokens=usage.input_tokens + usage.cache_read_input_tokens,
            output_tokens=usage.output_tokens,
            usd=usage.usd,
        )
        text = "".join(
            str(block.text) for block in response.content if getattr(block, "type", "") == "text"
        )
        return Completion(
            text=text,
            usage=usage,
            model=str(getattr(response, "model", model)),
            request=dict(params),
            response=_to_dict(response),
        )

    # -- offline: Message Batches (spec.md R18) ---------------------------------------

    def submit_batch(self, items: list[BatchItem]) -> str:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        requests = [
            Request(
                custom_id=item.custom_id,
                params=cast(MessageCreateParamsNonStreaming, item.params()),
            )
            for item in items
        ]
        batch = self._with_retry(lambda: self._client.messages.batches.create(requests=requests))
        return str(batch.id)

    def batch_status(self, batch_id: str) -> str:
        batch = self._with_retry(lambda: self._client.messages.batches.retrieve(batch_id))
        return str(batch.processing_status)

    def collect_batch(self, batch_id: str, items: list[BatchItem]) -> dict[str, BatchResult]:
        """Results keyed by custom id; errored, canceled and expired entries carry `error`."""
        by_id = {item.custom_id: item for item in items}
        out: dict[str, BatchResult] = {}
        for entry in self._with_retry(lambda: self._client.messages.batches.results(batch_id)):
            custom_id = str(entry.custom_id)
            kind = entry.result.type
            if kind != "succeeded":
                detail = getattr(getattr(entry.result, "error", None), "type", kind)
                out[custom_id] = BatchResult(custom_id, error=f"{kind}: {detail}")
                continue
            message = entry.result.message
            item = by_id.get(custom_id)
            text = "".join(str(b.text) for b in message.content if getattr(b, "type", "") == "text")
            parsed: BaseModel | None = None
            if item is not None and item.schema is not None:
                try:
                    parsed = item.schema.model_validate_json(text)
                except ValueError as exc:
                    out[custom_id] = BatchResult(custom_id, error=f"schema: {exc}")
                    continue
            usage = _usage(str(getattr(message, "model", "")), message.usage, batch=True)
            out[custom_id] = BatchResult(custom_id, text=text, parsed=parsed, usage=usage)
        return out


class BatchItem:
    """One request in a batch: the same layout as an online call, with an optional schema."""

    def __init__(
        self,
        custom_id: str,
        *,
        system: list[SystemBlock],
        user: str,
        model: str,
        max_tokens: int = 1024,
        schema: type[BaseModel] | None = None,
    ) -> None:
        self.custom_id = custom_id
        self.system = system
        self.user = user
        self.model = model
        self.max_tokens = max_tokens
        self.schema = schema

    def params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_param(self.system),
            "messages": [{"role": "user", "content": self.user}],
        }
        if self.schema is not None:
            params["output_config"] = {
                "format": {"type": "json_schema", "schema": self.schema.model_json_schema()}
            }
        return params


class BatchResult:
    def __init__(
        self,
        custom_id: str,
        *,
        text: str = "",
        parsed: BaseModel | None = None,
        usage: Usage | None = None,
        error: str | None = None,
    ) -> None:
        self.custom_id = custom_id
        self.text = text
        self.parsed = parsed
        self.usage = usage
        self.error = error


def _to_dict(response: Any) -> dict[str, Any]:
    to_dict = getattr(response, "to_dict", None)
    if callable(to_dict):
        raw = to_dict()
        if isinstance(raw, dict):
            return dict(raw)
    try:
        return dict(json.loads(json.dumps(response, default=str)))
    except (TypeError, ValueError):
        return {"repr": repr(response)}
