"""Reranker seam: Voyage rerank-2.5 behind RETRIEVAL_RERANK. An outage degrades to the
fused order with a guardrail event; it never fails an evaluation. The query and every
document are redacted before they leave (A-96), and Voyage needs the recorded opt-out."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from ccp import credentials
from ccp.config import Settings
from ccp.llm.errors import RetryableModelError, TerminalModelError, classify_status
from ccp.observability import cost
from ccp.retrieval.embeddings import require_voyage_terms
from ccp.security.secrets_patterns import redact, redact_all

RETRY_ATTEMPTS = 3


@dataclass(frozen=True)
class RerankHit:
    index: int  # position in the candidate list handed to rerank()
    score: float


@dataclass(frozen=True)
class RerankResult:
    hits: list[RerankHit]  # best first
    total_tokens: int
    usd: float


class Reranker(Protocol):
    model_id: str

    def rerank(self, query: str, documents: Sequence[str], top_k: int) -> RerankResult: ...


class RerankUnavailable(RetryableModelError):
    """The provider could not be reached; the caller keeps the fused order."""


class VoyageReranker:
    url = "https://api.voyageai.com/v1/rerank"

    def __init__(
        self,
        api_key: str,
        model: str = "rerank-2.5",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model_id = model
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._http = httpx.Client(timeout=60.0, transport=transport)
        self._sleep = sleep

    def rerank(self, query: str, documents: Sequence[str], top_k: int) -> RerankResult:
        body: dict[str, Any] = {
            "query": redact(query)[0],
            "documents": redact_all(list(documents)),
            "model": self.model_id,
            "top_k": top_k,
            "return_documents": False,
        }
        data: Any = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                response = self._http.post(self.url, headers=self._headers, json=body)
                response.raise_for_status()
                data = response.json()
                break
            except httpx.HTTPStatusError as exc:
                mapped = classify_status(exc.response.status_code, exc.response.text[:200])
                if isinstance(mapped, TerminalModelError) or attempt == RETRY_ATTEMPTS - 1:
                    raise mapped from exc
            except httpx.HTTPError as exc:
                if attempt == RETRY_ATTEMPTS - 1:
                    raise RerankUnavailable(f"{type(exc).__name__}: {exc}") from exc
            self._sleep(2**attempt + random.uniform(0, 0.5))  # noqa: S311
        hits = [
            RerankHit(int(d["index"]), float(d["relevance_score"]))
            for d in sorted(data["data"], key=lambda d: -float(d["relevance_score"]))
        ]
        tokens = int(data.get("usage", {}).get("total_tokens", 0))
        return RerankResult(hits, tokens, cost.estimate_usd(self.model_id, input_tokens=tokens))


class LexicalOverlapReranker:
    """Keyless stand-in: orders candidates by query-token overlap. Deterministic; used so the
    rerank path (top-20 -> top-6, rerank scores on the step) runs offline."""

    model_id = "local-overlap-v1"

    def rerank(self, query: str, documents: Sequence[str], top_k: int) -> RerankResult:
        terms = {t for t in query.lower().split() if len(t) > 2}
        scored: list[RerankHit] = []
        for i, doc in enumerate(documents):
            words = set(doc.lower().split())
            overlap = len(terms & words) / len(terms) if terms else 0.0
            scored.append(RerankHit(i, round(overlap, 6)))
        scored.sort(key=lambda h: (-h.score, h.index))
        return RerankResult(scored[:top_k], 0, 0.0)


def build_reranker(settings: Settings) -> Reranker | None:
    """None when the flag is off; Voyage when its key is set; the local stand-in otherwise."""
    if not settings.retrieval_rerank:
        return None
    key = credentials.secret(settings, "rerank:voyage")
    if key is not None:
        require_voyage_terms(settings)
        return VoyageReranker(key)
    return LexicalOverlapReranker()
