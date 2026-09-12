"""LLMClient: the one interface every model call goes through (spec.md section 2 conventions).

Phase 7a ships the interface, the budget hook, and a deterministic stub used by tests.
The Anthropic implementation arrives in Phase 7b. No module outside ``ccp.llm`` imports
the vendor SDK; the layering test enforces it.
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


class LLMClient(Protocol):
    provider: str

    def extract(
        self,
        *,
        schema: type[T],
        system: str,
        user: str,
        model: str,
        tracker: BudgetTracker,
        max_tokens: int = 2048,
    ) -> Extraction: ...


StubHandler = Callable[[type[Any], str, str], BaseModel]


@dataclass
class StubLLMClient:
    """Deterministic stand-in for tests and keyless runs: returns canned parsed objects."""

    provider: str = "stub"
    handler: StubHandler | None = None
    charge_tokens: tuple[int, int] = (500, 100)
    charge_usd: float = 0.001
    calls: list[dict[str, Any]] = field(default_factory=list)

    def extract(
        self,
        *,
        schema: type[T],
        system: str,
        user: str,
        model: str,
        tracker: BudgetTracker,
        max_tokens: int = 2048,
    ) -> Extraction:
        tracker.charge(
            input_tokens=self.charge_tokens[0],
            output_tokens=self.charge_tokens[1],
            usd=self.charge_usd,
        )
        if self.handler is None:
            raise RuntimeError("StubLLMClient has no handler for this call")
        parsed = self.handler(schema, system, user)
        request = {"model": model, "system": system, "user": user, "schema": schema.__name__}
        response = {"parsed": parsed.model_dump(mode="json")}
        self.calls.append(request)
        return Extraction(
            parsed=parsed,
            usage=Usage(*self.charge_tokens, usd=self.charge_usd),
            model=f"stub:{model}",
            request=request,
            response=response,
        )


def build_client(settings: Settings, stub: StubLLMClient | None = None) -> LLMClient:
    """Provider selection. The Anthropic client is wired in Phase 7b; until then a stub
    must be supplied explicitly, so a keyless run can never pretend a model answered."""
    if stub is not None:
        return stub
    if settings.anthropic_api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; AI-tier evaluation is unavailable")
    raise NotImplementedError("the Anthropic client arrives in Phase 7b")
