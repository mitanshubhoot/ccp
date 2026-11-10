"""EmbeddingClient: every embedding call goes through here (spec.md A-14, conventions).

    VoyageEmbeddingClient    voyage-3-large at 1024 dimensions (default when VOYAGE_API_KEY is set)
    OpenAIEmbeddingClient    text-embedding-3-large at 1024 dimensions (CCP_EMBED_PROVIDER=openai)
    HashingEmbeddingClient   local, keyless, deterministic feature hashing: the stand-in that
                             keeps the whole pipeline runnable offline. Its model id is stored
                             on every chunk it embeds, like any other model, so retrieval never
                             mixes it with a hosted model's vectors.

Hosted calls are plain HTTPS through httpx (no vendor SDK); failures map onto the model
error taxonomy and retry with backoff before surfacing. Every text a hosted client sends is
redacted first, at the client itself, so no caller can forget to (A-96); and Voyage is
refused until the operator records the training opt-out (VOYAGE_TRAINING_OPT_OUT=true).
"""

from __future__ import annotations

import hashlib
import math
import random
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from ccp.config import Settings
from ccp.llm.errors import RetryableModelError, TerminalModelError, classify_status
from ccp.observability import cost
from ccp.security.secrets_patterns import redact_all


class ProviderTermsNotRecorded(RuntimeError):
    """A provider whose terms need an operator action was configured before that action was
    recorded. Voyage may train on self-serve data unless the organization opts out."""


def require_voyage_terms(settings: Settings) -> None:
    if not settings.voyage_training_opt_out:
        raise ProviderTermsNotRecorded(
            "VOYAGE_API_KEY is set but the Voyage training opt-out is not recorded: opt out in"
            " the Voyage organization settings, then set VOYAGE_TRAINING_OPT_OUT=true"
        )


DIMENSION = 1024
InputType = Literal["document", "query"]
_TOKEN = re.compile(r"[a-z0-9]+")
RETRY_ATTEMPTS = 3


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    total_tokens: int
    usd: float


class EmbeddingClient(Protocol):
    model_id: str
    dimension: int

    def embed(self, texts: Sequence[str], input_type: InputType) -> EmbeddingResult: ...


class EmbeddingUnavailable(RetryableModelError):
    """The provider could not be reached; retrieval degrades to the lexical arm."""


def _retry(call: Callable[[], Any], sleep: Callable[[float], None]) -> Any:
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return call()
        except httpx.HTTPStatusError as exc:
            mapped = classify_status(exc.response.status_code, exc.response.text[:200])
            if isinstance(mapped, TerminalModelError) or attempt == RETRY_ATTEMPTS - 1:
                raise mapped from exc
        except httpx.HTTPError as exc:
            if attempt == RETRY_ATTEMPTS - 1:
                raise EmbeddingUnavailable(f"{type(exc).__name__}: {exc}") from exc
        sleep(2**attempt + random.uniform(0, 0.5))  # noqa: S311
    raise EmbeddingUnavailable("unreachable")


class HashingEmbeddingClient:
    """Feature hashing over unigrams and bigrams, signed, L2-normalized. Cosine similarity
    then measures lexical overlap, which is enough to exercise every retrieval path."""

    model_id = "local-hashing-v1"
    dimension = DIMENSION

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        tokens = _TOKEN.findall(text.lower())
        features = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:], strict=False)]
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            weight = 0.5 if "_" in feature else 1.0
            vec[index] += sign * weight
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    def embed(self, texts: Sequence[str], input_type: InputType) -> EmbeddingResult:
        return EmbeddingResult([self._vector(t) for t in texts], total_tokens=0, usd=0.0)


class VoyageEmbeddingClient:
    dimension = DIMENSION
    url = "https://api.voyageai.com/v1/embeddings"
    batch_size = 128

    def __init__(
        self,
        api_key: str,
        model: str = "voyage-3-large",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model_id = model
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._http = httpx.Client(timeout=60.0, transport=transport)
        self._sleep = sleep

    def embed(self, texts: Sequence[str], input_type: InputType) -> EmbeddingResult:
        vectors: list[list[float]] = []
        tokens = 0
        for start in range(0, len(texts), self.batch_size):
            batch = redact_all(list(texts[start : start + self.batch_size]))
            body = {
                "input": batch,
                "model": self.model_id,
                "input_type": input_type,
                "output_dimension": self.dimension,
            }

            def call(body: dict[str, Any] = body) -> Any:
                response = self._http.post(self.url, headers=self._headers, json=body)
                response.raise_for_status()
                return response.json()

            data = _retry(call, self._sleep)
            ordered = sorted(data["data"], key=lambda d: int(d["index"]))
            vectors.extend([float(x) for x in d["embedding"]] for d in ordered)
            tokens += int(data.get("usage", {}).get("total_tokens", 0))
        return EmbeddingResult(
            vectors, tokens, cost.estimate_usd(self.model_id, input_tokens=tokens)
        )


class OpenAIEmbeddingClient:
    dimension = DIMENSION
    url = "https://api.openai.com/v1/embeddings"
    batch_size = 128

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-large",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model_id = model
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._http = httpx.Client(timeout=60.0, transport=transport)
        self._sleep = sleep

    def embed(self, texts: Sequence[str], input_type: InputType) -> EmbeddingResult:
        vectors: list[list[float]] = []
        tokens = 0
        for start in range(0, len(texts), self.batch_size):
            batch = redact_all(list(texts[start : start + self.batch_size]))
            body = {"input": batch, "model": self.model_id, "dimensions": self.dimension}

            def call(body: dict[str, Any] = body) -> Any:
                response = self._http.post(self.url, headers=self._headers, json=body)
                response.raise_for_status()
                return response.json()

            data = _retry(call, self._sleep)
            ordered = sorted(data["data"], key=lambda d: int(d["index"]))
            vectors.extend([float(x) for x in d["embedding"]] for d in ordered)
            tokens += int(data.get("usage", {}).get("total_tokens", 0))
        return EmbeddingResult(
            vectors, tokens, cost.estimate_usd(self.model_id, input_tokens=tokens)
        )


def build_embedding_client(settings: Settings) -> EmbeddingClient:
    """The configured hosted provider when its key is set; the local hashing client otherwise."""
    if settings.ccp_embed_provider == "openai" and settings.openai_api_key is not None:
        return OpenAIEmbeddingClient(
            settings.openai_api_key.get_secret_value(), settings.ccp_embed_model
        )
    if settings.ccp_embed_provider == "voyage" and settings.voyage_api_key is not None:
        require_voyage_terms(settings)
        return VoyageEmbeddingClient(
            settings.voyage_api_key.get_secret_value(), settings.ccp_embed_model
        )
    return HashingEmbeddingClient()


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector input syntax."""
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"


def parse_vector(text: str) -> list[float]:
    return [float(x) for x in text.strip("[]").split(",") if x]
