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

Models and version (A-103): every call names a pinned model id from ccp/llm/models.py,
which also says how that model turns thinking off and takes effort; no call sends
temperature, top_p or top_k; and the client sends anthropic-version
ANTHROPIC_API_VERSION itself, so an SDK upgrade cannot change it.

Prompt layout (spec.md R16): `system` blocks are stable-first and the block flagged
`cache=True` carries an explicit `cache_control` breakpoint; per-call material lives in the
user turn. Note the model-specific minimum cacheable prefix (4096 tokens on Haiku 4.5,
512 on Haiku 5.5): a shorter prefix silently does not cache.
"""

from __future__ import annotations

import base64
import json
import random
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar, cast

import anthropic
from pydantic import BaseModel

from ccp import credentials
from ccp.config import Settings
from ccp.llm.budget import BudgetTracker
from ccp.llm.client import (
    Attachment,
    ChatTurn,
    Completion,
    Extraction,
    SystemBlock,
    ToolCall,
    Usage,
)
from ccp.llm.errors import (
    RetryableModelError,
    TerminalModelError,
    UnpricedModel,
    classify_status,
)
from ccp.llm.models import ANTHROPIC_API_VERSION, sampling_params, spec_for, thinks
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


# max_tokens bounds the thinking and the answer together; a structured answer behind a
# 2048-token cap would be cut off mid-JSON on a live reasoning model (a stub never is).
REASONING_MIN_MAX_TOKENS = 8192


def _room_for_thinking(model: str, params: dict[str, Any]) -> None:
    """A call that can think gets room to think and still answer: adaptive thinking asked
    for, or a model that thinks unless told not to (Haiku 5.5 and the 5.x models think
    when `thinking` is left out; Opus 5.5 and Fable always think)."""
    if thinks(model, params):
        params["max_tokens"] = max(int(params["max_tokens"]), REASONING_MIN_MAX_TOKENS)


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


def _context(usage: Usage) -> int:
    """Everything one call put through the model: what the token cap bounds (A-100)."""
    return (
        usage.input_tokens
        + usage.cache_read_input_tokens
        + usage.cache_creation_input_tokens
        + usage.output_tokens
    )


def _attachment_block(a: Attachment) -> dict[str, Any]:
    data = base64.standard_b64encode(a.data).decode("ascii")
    if a.media_type == "application/pdf":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": a.media_type, "data": data},
            "title": a.name,
        }
    return {"type": "image", "source": {"type": "base64", "media_type": a.media_type, "data": data}}


def _user_content(
    user: str, documents: Sequence[str], attachments: Sequence[Attachment] = ()
) -> str | list[dict[str, Any]]:
    """Evidence and tool output as text document blocks ahead of the instruction, never
    pasted into it (A-98), after any image or PDF the model reads (A-147); a call with
    neither keeps the plain string."""
    if not documents and not attachments:
        return user
    blocks: list[dict[str, Any]] = [_attachment_block(a) for a in attachments]
    blocks += [
        {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": document},
            "title": f"evidence {n}",
        }
        for n, document in enumerate(documents, start=1)
    ]
    return [*blocks, {"type": "text", "text": user}]


def _loggable(params: dict[str, Any], attachments: Sequence[Attachment]) -> dict[str, Any]:
    """The request as recorded: an attachment by name, type and digest, never its bytes."""
    if not attachments:
        return params
    return {**params, "messages": "redacted", "attachments": [a.describe() for a in attachments]}


def _require_price(model: str) -> None:
    """A model the price table does not know would be charged $0, so no dollar cap could
    stop it: refused before any call (A-97). An alias that can move, or an id the model
    table does not know, is refused next (A-103): pinned ids only."""
    if not cost.is_priced(model):
        raise UnpricedModel(
            f"no price for model {model!r} in ccp/observability/cost.py; add it before use"
        )
    spec_for(model)


def _classify(exc: Exception) -> Exception:
    if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
        return RetryableModelError(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, anthropic.APIStatusError):
        return classify_status(exc.status_code, getattr(exc, "message", str(exc)))
    return TerminalModelError(f"{type(exc).__name__}: {exc}")


class _Malformed:
    """The model answered, but not in the schema: the SDK raised while parsing the text.
    Carries no usage (the SDK discarded the response) and no parsed output."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.model: str | None = None
        self.parsed_output: Any = None
        self.stop_reason = "malformed"

    def to_dict(self) -> dict[str, Any]:
        return {"malformed": self.reason}


def _block_dict(block: Any) -> dict[str, Any]:
    """A content block as the API accepts it back (signed thinking blocks must return
    verbatim when a tool-use turn continues)."""
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        return dict(dump(exclude_none=True))
    return dict(block)


class AnthropicClient:
    provider = "anthropic"

    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if client is None:
            key = credentials.secret(settings, "model:anthropic")
            if key is None:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            client = anthropic.Anthropic(
                api_key=key,
                max_retries=0,
                default_headers={"anthropic-version": ANTHROPIC_API_VERSION},
            )
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

    def _stream_parse(
        self, params: dict[str, Any], schema: type[Any], on_token: Callable[[str], None]
    ) -> Any:
        """Structured output streamed token by token (spec.md Layer 12); the final message
        carries parsed_output exactly as the non-streamed call does. Once a token has
        reached the caller the stream cannot be retried transparently, so a failure after
        that point is terminal rather than replayed into the terminal."""
        emitted = False
        try:
            with self._client.messages.stream(**params, output_format=schema) as stream:
                for text in stream.text_stream:
                    emitted = True
                    on_token(text)
                try:
                    return stream.get_final_message()
                except ValueError as exc:
                    return _Malformed(f"{type(exc).__name__}: {exc}"[:500])
        except TerminalModelError:
            raise
        except Exception as exc:
            if emitted:
                raise TerminalModelError(
                    f"stream interrupted after output began: {type(exc).__name__}: {exc}"
                ) from exc
            raise

    def extract(
        self,
        *,
        schema: type[T],
        system: list[SystemBlock],
        user: str,
        model: str,
        tracker: BudgetTracker,
        max_tokens: int = 2048,
        reasoning: bool = False,
        on_token: Callable[[str], None] | None = None,
        documents: Sequence[str] = (),
        attachments: Sequence[Attachment] = (),
    ) -> Extraction:
        _require_price(model)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_param(system),
            "messages": [{"role": "user", "content": _user_content(user, documents, attachments)}],
            **sampling_params(model, reasoning=reasoning),
        }
        _room_for_thinking(model, params)
        request = {**_loggable(params, attachments), "output_format": schema.__name__}
        if on_token is not None:
            response = self._with_retry(lambda: self._stream_parse(params, schema, on_token))
        else:
            response = self._with_retry(lambda: self._parse_or_malformed(params, schema))
        usage, parsed = self._settle(model, tracker, response)
        if parsed is None:  # one re-ask, then terminal (spec.md D9)
            params["messages"] = [
                {
                    "role": "user",
                    "content": _user_content(user + REASK_SUFFIX, documents, attachments),
                }
            ]
            response = self._with_retry(lambda: self._parse_or_malformed(params, schema))
            second, parsed = self._settle(model, tracker, response)
            usage = Usage(
                usage.input_tokens + second.input_tokens,
                usage.output_tokens + second.output_tokens,
                usage.cache_read_input_tokens + second.cache_read_input_tokens,
                usage.cache_creation_input_tokens + second.cache_creation_input_tokens,
                usage.usd + second.usd,
            )
            if parsed is None:
                raise TerminalModelError("schema refusal after one re-ask")
        return Extraction(
            parsed=parsed,
            usage=usage,
            model=str(getattr(response, "model", None) or model),
            request=request,
            response=_to_dict(response),
        )

    def _parse_or_malformed(self, params: dict[str, Any], schema: type[Any]) -> Any:
        """The SDK raises when the answer is not valid JSON for the schema; that is a
        malformed answer to ask again about, not a provider error to retry."""
        try:
            return self._client.messages.parse(**params, output_format=schema)
        except ValueError as exc:  # pydantic's ValidationError is a ValueError
            return _Malformed(f"{type(exc).__name__}: {exc}"[:500])

    def _settle(self, model: str, tracker: BudgetTracker, response: Any) -> tuple[Usage, Any]:
        """Charge the tracker and pick the parsed object; None means ask once more."""
        if isinstance(response, _Malformed):
            return Usage(0, 0, 0, 0, 0.0), None
        usage = _usage(model, response.usage)
        tracker.charge(
            input_tokens=usage.input_tokens + usage.cache_read_input_tokens,
            output_tokens=usage.output_tokens,
            usd=usage.usd,
            context_tokens=_context(usage),
        )
        if getattr(response, "stop_reason", None) == "refusal":
            raise TerminalModelError("model refused the extraction request")
        return usage, getattr(response, "parsed_output", None)

    def complete(
        self,
        *,
        system: list[SystemBlock],
        user: str,
        model: str,
        tracker: BudgetTracker,
        max_tokens: int = 512,
    ) -> Completion:
        _require_price(model)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_param(system),
            "messages": [{"role": "user", "content": user}],
            **sampling_params(model, reasoning=False),
        }
        _room_for_thinking(model, params)
        response = self._with_retry(lambda: self._client.messages.create(**params))
        usage = _usage(model, response.usage)
        tracker.charge(
            input_tokens=usage.input_tokens + usage.cache_read_input_tokens,
            output_tokens=usage.output_tokens,
            usd=usage.usd,
            context_tokens=_context(usage),
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

    def chat(
        self,
        *,
        system: list[SystemBlock],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        tracker: BudgetTracker,
        max_tokens: int = 1024,
        reasoning: bool = False,
    ) -> ChatTurn:
        _require_price(model)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_param(system),
            "messages": messages,
            **sampling_params(model, reasoning=reasoning),
        }
        _room_for_thinking(model, params)
        if tools:
            params["tools"] = tools
        response = self._with_retry(lambda: self._client.messages.create(**params))
        usage = _usage(model, response.usage)
        tracker.charge(
            input_tokens=usage.input_tokens + usage.cache_read_input_tokens,
            output_tokens=usage.output_tokens,
            usd=usage.usd,
            context_tokens=_context(usage),
        )
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        content: list[dict[str, Any]] = []
        for block in response.content:
            kind = getattr(block, "type", "")
            if kind == "text":
                text_parts.append(str(block.text))
                content.append({"type": "text", "text": str(block.text)})
            elif kind in ("thinking", "redacted_thinking"):
                content.append(_block_dict(block))
            elif kind == "tool_use":
                arguments = dict(block.input) if isinstance(block.input, dict) else {}
                calls.append(ToolCall(str(block.id), str(block.name), arguments))
                content.append(
                    {
                        "type": "tool_use",
                        "id": str(block.id),
                        "name": str(block.name),
                        "input": arguments,
                    }
                )
        return ChatTurn(
            text="".join(text_parts),
            tool_calls=tuple(calls),
            stop_reason=str(getattr(response, "stop_reason", "")),
            usage=usage,
            model=str(getattr(response, "model", model)),
            request={**params, "tools": [t.get("name") for t in tools]},
            response=_to_dict(response),
            assistant_content=content,
        )

    # -- offline: Message Batches (spec.md R18) ---------------------------------------

    def submit_batch(self, items: list[BatchItem]) -> str:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        for item in items:
            _require_price(item.model)
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
            **sampling_params(self.model, reasoning=False),
        }
        _room_for_thinking(self.model, params)
        if self.schema is not None:
            params["output_config"] = {
                **params.get("output_config", {}),
                "format": {"type": "json_schema", "schema": self.schema.model_json_schema()},
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
