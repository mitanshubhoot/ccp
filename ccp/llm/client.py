"""LLMClient: the one interface every model call goes through (spec.md section 2 conventions).

    extract(schema, system, user, model, tracker)   -> Extraction   structured output
    complete(system, user, model, tracker)          -> Completion   plain text (enrichment)
    chat(system, messages, tools, model, tracker)   -> ChatTurn     one turn of a tool loop

`system` is a list of SystemBlock laid out stable-first; the block flagged `cache=True`
carries the prompt-cache breakpoint. Per-call material (the fence nonce, the evidence)
belongs in `user`, after the breakpoint. `reasoning=True` asks for adaptive thinking at
medium effort on models that support it (tier 3). No module outside ``ccp.llm`` imports
the vendor SDK; the layering test enforces it. The stub client is the keyless stand-in.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from ccp.config import Settings
from ccp.llm.budget import BudgetTracker

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class SystemBlock:
    text: str
    cache: bool = False  # the last stable block carries the breakpoint


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    usd: float = 0.0


@dataclass(frozen=True)
class Extraction:
    """A structured-output result: the parsed model, the usage, and the raw request/response."""

    parsed: Any
    usage: Usage
    model: str
    request: dict[str, Any]
    response: dict[str, Any]


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage
    model: str
    request: dict[str, Any]
    response: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatTurn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str
    usage: Usage
    model: str
    request: dict[str, Any]
    response: dict[str, Any]
    assistant_content: list[dict[str, Any]]  # the blocks to append to the history


class LLMClient(Protocol):
    provider: str

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
    ) -> Extraction: ...

    def complete(
        self,
        *,
        system: list[SystemBlock],
        user: str,
        model: str,
        tracker: BudgetTracker,
        max_tokens: int = 512,
    ) -> Completion: ...

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
    ) -> ChatTurn: ...


def blocks_as_json(system: list[SystemBlock]) -> list[dict[str, Any]]:
    return [{"text": b.text, "cache": b.cache} for b in system]


def tool_result_message(tool_call_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """The user-turn block that answers one tool call."""
    import json

    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": json.dumps(result, default=str)[:20_000],
                "is_error": bool(result.get("is_error")),
            }
        ],
    }


StubExtractHandler = Callable[[type[Any], list[SystemBlock], str], BaseModel]
StubCompleteHandler = Callable[[list[SystemBlock], str], str]
StubChatHandler = Callable[
    [list[SystemBlock], list[dict[str, Any]], list[dict[str, Any]]],
    tuple[str, list[ToolCall]],
]


@dataclass
class StubLLMClient:
    """Deterministic stand-in for tests and keyless runs: returns canned parsed objects."""

    provider: str = "stub"
    handler: StubExtractHandler | None = None
    complete_handler: StubCompleteHandler | None = None
    chat_handler: StubChatHandler | None = None
    charge_tokens: tuple[int, int] = (500, 100)
    charge_usd: float = 0.001
    calls: list[dict[str, Any]] = field(default_factory=list)

    def _charge(self, tracker: BudgetTracker) -> Usage:
        tracker.charge(
            input_tokens=self.charge_tokens[0],
            output_tokens=self.charge_tokens[1],
            usd=self.charge_usd,
        )
        return Usage(*self.charge_tokens, usd=self.charge_usd)

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
    ) -> Extraction:
        usage = self._charge(tracker)
        if self.handler is None:
            raise RuntimeError("StubLLMClient has no extract handler for this call")
        parsed = self.handler(schema, system, user)
        if on_token is not None:  # stream the JSON in a few deltas, as a live model would
            text = json.dumps(parsed.model_dump(mode="json"))
            size = max(1, -(-len(text) // 4))
            for start in range(0, len(text), size):
                on_token(text[start : start + size])
        request = {
            "model": model,
            "system": blocks_as_json(system),
            "user": user,
            "schema": schema.__name__,
            "max_tokens": max_tokens,
            "reasoning": reasoning,
        }
        response = {"parsed": parsed.model_dump(mode="json")}
        self.calls.append(request)
        return Extraction(
            parsed=parsed, usage=usage, model=f"stub:{model}", request=request, response=response
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
        usage = self._charge(tracker)
        if self.complete_handler is None:
            raise RuntimeError("StubLLMClient has no complete handler for this call")
        text = self.complete_handler(system, user)
        request = {
            "model": model,
            "system": blocks_as_json(system),
            "user": user,
            "max_tokens": max_tokens,
        }
        self.calls.append(request)
        return Completion(
            text=text, usage=usage, model=f"stub:{model}", request=request, response={"text": text}
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
        usage = self._charge(tracker)
        if self.chat_handler is None:
            raise RuntimeError("StubLLMClient has no chat handler for this call")
        text, calls = self.chat_handler(system, messages, tools)
        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        for call in calls:
            content.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            )
        request = {
            "model": model,
            "system": blocks_as_json(system),
            "messages": messages,
            "tools": [t["name"] for t in tools],
            "max_tokens": max_tokens,
        }
        self.calls.append(request)
        return ChatTurn(
            text=text,
            tool_calls=tuple(calls),
            stop_reason="tool_use" if calls else "end_turn",
            usage=usage,
            model=f"stub:{model}",
            request=request,
            response={"content": content},
            assistant_content=content,
        )


def build_client(settings: Settings, stub: StubLLMClient | None = None) -> LLMClient:
    """Provider selection: an explicit stub, else the Anthropic client when a key is set."""
    if stub is not None:
        return stub
    if settings.anthropic_api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; AI-tier evaluation is unavailable")
    from ccp.llm.anthropic_client import AnthropicClient

    return AnthropicClient(settings)


def try_build_client(settings: Settings) -> LLMClient | None:
    """None when no provider is configured; callers park AI-tier work instead of failing."""
    if settings.anthropic_api_key is None:
        return None
    return build_client(settings)
