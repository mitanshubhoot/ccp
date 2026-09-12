"""USD-per-million-token price table (spec.md Layer 12). The step logger and the budget
tracker multiply; `cost_basis` is always "anthropic_list" in this pass.

Prices are the published list prices on the date below. Re-check them when a model id
changes; a wrong number here mis-states spend but never changes a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass

CHECKED_ON = "2026-09-11"
COST_BASIS = "anthropic_list"
BATCH_MULTIPLIER = 0.5


@dataclass(frozen=True)
class Price:
    input: float  # USD per million tokens
    output: float = 0.0
    cache_write: float = 0.0  # 5-minute TTL
    cache_read: float = 0.0


PRICES: dict[str, Price] = {
    # Claude (platform.claude.com/docs/en/about-claude/pricing)
    "claude-haiku-4-5": Price(input=1.0, output=5.0, cache_write=1.25, cache_read=0.10),
    "claude-sonnet-5": Price(input=2.0, output=10.0, cache_write=2.50, cache_read=0.20),
    "claude-opus-5": Price(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50),
    "claude-fable-5-1": Price(input=10.0, output=50.0, cache_write=12.50, cache_read=0.25),
    # Voyage (docs.voyageai.com/docs/pricing)
    "voyage-3-large": Price(input=0.18),
    "voyage-3.5": Price(input=0.06),
    "rerank-2.5": Price(input=0.05),
    "rerank-2.5-lite": Price(input=0.02),
    # OpenAI embeddings, list price as last seen; verify before relying on it
    "text-embedding-3-large": Price(input=0.13),
    # local providers cost nothing
    "local-hashing-v1": Price(input=0.0),
}


def price_for(model: str) -> Price:
    """Exact id first, then the longest known prefix (dated ids such as -20260101)."""
    if model in PRICES:
        return PRICES[model]
    for known in sorted(PRICES, key=len, reverse=True):
        if model.startswith(known):
            return PRICES[known]
    return Price(input=0.0)


def estimate_usd(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    batch: bool = False,
) -> float:
    p = price_for(model)
    usd = (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_read_tokens * p.cache_read
        + cache_write_tokens * p.cache_write
    ) / 1_000_000
    return usd * BATCH_MULTIPLIER if batch else usd
