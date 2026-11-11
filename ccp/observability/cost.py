"""USD-per-million-token price table (spec.md Layer 12). The step logger and the budget
tracker multiply; `cost_basis` is always "anthropic_list" in this pass.

Prices are the published list prices on the date below. Re-check them when a model id
changes; a wrong number here mis-states spend but never changes a verdict. A model whose
price depends on prompt length (Haiku 5.5 above 100K prompt tokens) carries the second
rate card in `long_prompt`; the prompt counts cached and uncached input alike.
"""

from __future__ import annotations

from dataclasses import dataclass

CHECKED_ON = "2026-11-10"
COST_BASIS = "anthropic_list"
BATCH_MULTIPLIER = 0.5
LONG_PROMPT_TOKENS = 100_000


@dataclass(frozen=True)
class Price:
    input: float  # USD per million tokens
    output: float = 0.0
    cache_write: float = 0.0  # 5-minute TTL
    cache_read: float = 0.0
    long_prompt: Price | None = None  # the rate card above LONG_PROMPT_TOKENS of prompt


PRICES: dict[str, Price] = {
    # Claude (platform.claude.com/docs/en/about-claude/pricing)
    "claude-haiku-4-5": Price(input=1.0, output=5.0, cache_write=1.25, cache_read=0.10),
    "claude-haiku-5-5": Price(
        input=0.10,
        output=0.50,
        cache_write=0.125,
        cache_read=0.01,
        long_prompt=Price(input=0.50, output=2.50, cache_write=0.625, cache_read=0.05),
    ),
    "claude-sonnet-5": Price(input=2.0, output=10.0, cache_write=2.50, cache_read=0.20),
    "claude-sonnet-5-5": Price(input=2.0, output=10.0, cache_write=2.50, cache_read=0.20),
    "claude-opus-5": Price(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50),
    "claude-opus-5-5": Price(input=4.0, output=20.0, cache_write=5.00, cache_read=0.20),
    "claude-fable-5-1": Price(input=10.0, output=50.0, cache_write=12.50, cache_read=0.25),
    # Voyage (docs.voyageai.com/docs/pricing)
    "voyage-3-large": Price(input=0.18),
    "voyage-3.5": Price(input=0.06),
    "voyage-4-large": Price(input=0.12),  # 1,024 dimensions, as the schema keeps (A-149)
    "rerank-2.5": Price(input=0.05),
    "rerank-2.5-lite": Price(input=0.02),
    "rerank-3": Price(input=0.05),  # the same price as rerank-2.5
    # OpenAI embeddings, list price as last seen; verify before relying on it
    "text-embedding-3-large": Price(input=0.13),
    # local providers cost nothing
    "local-hashing-v1": Price(input=0.0),
}


def _known(model: str) -> str | None:
    """Exact id first, then the longest known prefix (dated ids such as -20260101)."""
    if model in PRICES:
        return model
    for known in sorted(PRICES, key=len, reverse=True):
        if model.startswith(known):
            return known
    return None


def is_priced(model: str) -> bool:
    """False for a model this table does not know. The model clients refuse such a model
    before calling it, because its spend would count as $0 and no dollar cap could stop it
    (A-97); here, for reporting, it still prices as zero."""
    return _known(model) is not None


def price_for(model: str) -> Price:
    known = _known(model)
    return PRICES[known] if known is not None else Price(input=0.0)


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
    prompt = input_tokens + cache_read_tokens + cache_write_tokens
    if p.long_prompt is not None and prompt > LONG_PROMPT_TOKENS:
        p = p.long_prompt
    usd = (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_read_tokens * p.cache_read
        + cache_write_tokens * p.cache_write
    ) / 1_000_000
    return usd * BATCH_MULTIPLIER if batch else usd
