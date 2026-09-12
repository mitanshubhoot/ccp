"""LLMClient: the one interface every model call goes through (spec.md section 2 conventions).

    extract(schema, system, user, model, tracker)  -> Extraction   structured output
    complete(system, user, model, tracker)         -> Completion   plain text (enrichment)

`system` is a list of SystemBlock laid out stable-first; the block flagged `cache=True`
carries the prompt-cache breakpoint. Per-call material (the fence nonce, the evidence)
belongs in `user`, after the breakpoint. No module outside ``ccp.llm`` imports the vendor
SDK; the layering test enforces it. The stub client is the keyless stand-in for tests.
"""

from __future__ import annotations

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


def blocks_as_json(system: list[SystemBlock]) -> list[dict[str, Any]]:
    return [{"text": b.text, "cache": b.cache} for b in system]


StubExtractHandler = Callable[[type[Any], list[SystemBlock], str], BaseModel]
StubCompleteHandler = Callable[[list[SystemBlock], str], str]


@dataclass
class StubLLMClient:
    """Deterministic stand-in for tests and keyless runs: returns canned parsed objects."""

    provider: str = "stub"
    handler: StubExtractHandler | None = None
    complete_handler: StubCompleteHandler | None = None
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
    ) -> Extraction:
        usage = self._charge(tracker)
        if self.handler is None:
            raise RuntimeError("StubLLMClient has no extract handler for this call")
        parsed = self.handler(schema, system, user)
        request = {
            "model": model,
            "system": blocks_as_json(system),
            "user": user,
            "schema": schema.__name__,
            "max_tokens": max_tokens,
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
