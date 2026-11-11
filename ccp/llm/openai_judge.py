"""A judge from another model family, for the fixture suites only (Decision 9, spec.md A-103).

A grader from the same family as the model it grades shares its blind spots, so offline,
where every case is synthetic, the judge may come from another vendor. On customer data the
judge stays a different Claude model: only Anthropic processes customer evidence. The eval
runner enforces that boundary (``judge.DataBoundary``); this client only grades.

It speaks the Chat Completions protocol (``/v1/chat/completions`` with a strict JSON-schema
response format), which OpenAI serves and other vendors accept, over httpx like the OpenAI
embedding client. Text is redacted before it leaves (A-96), a model with no price is
refused before any call (A-97), 429, 5xx and transport errors retry with backoff, a refusal
or a cut-off answer is terminal, and an answer outside the schema is asked once more.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from ccp.llm.budget import BudgetTracker
from ccp.llm.client import (
    Attachment,
    ChatTurn,
    Completion,
    Extraction,
    SystemBlock,
    Usage,
    with_documents,
)
from ccp.llm.errors import (
    RetryableModelError,
    TerminalModelError,
    UnpricedModel,
    classify_status,
)
from ccp.llm.models import family
from ccp.observability import cost
from ccp.security.secrets_patterns import redact_all

T = TypeVar("T", bound=BaseModel)

URL = "https://api.openai.com/v1/chat/completions"
RETRY_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.0
REASK_SUFFIX = (
    "\n\nYour previous answer did not match the required schema. Answer again with only the "
    "fields of the schema, every required field present."
)


class CrossFamilyJudgeClient:
    """The judge seam for a non-Claude model: ``extract`` only."""

    provider = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        url: str = URL,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.url = url
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._http = httpx.Client(timeout=120.0, transport=transport)
        self._sleep = sleep

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                response = self._http.post(self.url, headers=self._headers, json=body)
            except httpx.TransportError as exc:
                last = RetryableModelError(f"{type(exc).__name__}: {exc}")
            else:
                if response.status_code < 400:
                    data = response.json()
                    if not isinstance(data, dict):
                        raise TerminalModelError("judge response is not a JSON object")
                    return data
                mapped = classify_status(response.status_code, response.text[:300])
                if not isinstance(mapped, RetryableModelError):
                    raise mapped
                last = mapped
            if attempt < RETRY_ATTEMPTS - 1:
                self._sleep(RETRY_BASE_SECONDS * (2**attempt) + random.uniform(0, 0.5))  # noqa: S311
        assert last is not None
        raise last

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
        if attachments:  # the judge reads text; a scan is transcribed first (A-147)
            raise TerminalModelError("the OpenAI judge takes no attachments")
        if family(model) == "anthropic":
            raise TerminalModelError(f"{model!r} is a Claude model; use the Anthropic client")
        if not cost.is_priced(model):
            raise UnpricedModel(
                f"no price for model {model!r} in ccp/observability/cost.py; add it before use"
            )
        text = redact_all([with_documents(user, list(documents))])[0]
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": "\n\n".join(b.text for b in system)},
                {"role": "user", "content": text},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                },
            },
            "max_completion_tokens": max_tokens,
        }
        total = Usage(0, 0, 0, 0, 0.0)
        data: dict[str, Any] = {}
        for attempt in range(2):  # one re-ask on an answer outside the schema
            data = self._post(body)
            usage = self._charge(model, tracker, data)
            total = Usage(
                total.input_tokens + usage.input_tokens,
                total.output_tokens + usage.output_tokens,
                total.cache_read_input_tokens + usage.cache_read_input_tokens,
                0,
                total.usd + usage.usd,
            )
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            if message.get("refusal"):
                raise TerminalModelError("model refused the judge request")
            if choice.get("finish_reason") == "length":
                raise TerminalModelError("judge answer cut off at max_tokens")
            try:
                parsed = schema.model_validate_json(str(message.get("content") or ""))
            except ValueError:
                if attempt == 0:
                    body["messages"][1]["content"] = text + REASK_SUFFIX
                    continue
                raise TerminalModelError("schema refusal after one re-ask") from None
            return Extraction(
                parsed=parsed,
                usage=total,
                model=str(data.get("model") or model),
                request={**body, "output_format": schema.__name__},
                response=data,
            )
        raise TerminalModelError("schema refusal after one re-ask")  # pragma: no cover

    def _charge(self, model: str, tracker: BudgetTracker, data: dict[str, Any]) -> Usage:
        raw = data.get("usage") or {}
        prompt = int(raw.get("prompt_tokens") or 0)
        completion = int(raw.get("completion_tokens") or 0)
        cached = int((raw.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        usd = cost.estimate_usd(
            model,
            input_tokens=prompt - cached,
            output_tokens=completion,
            cache_read_tokens=cached,
        )
        tracker.charge(
            input_tokens=prompt,
            output_tokens=completion,
            usd=usd,
            context_tokens=prompt + completion,
        )
        return Usage(prompt - cached, completion, cached, 0, usd)

    def complete(
        self,
        *,
        system: list[SystemBlock],
        user: str,
        model: str,
        tracker: BudgetTracker,
        max_tokens: int = 512,
    ) -> Completion:
        raise TerminalModelError("the cross-family judge only grades")

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
        raise TerminalModelError("the cross-family judge only grades")
