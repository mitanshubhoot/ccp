"""Context ceilings per call type (spec.md A-153, report 6.7 "A context budget per call type").

config/context_ceilings.yaml     the input ceiling of every call type, in tokens, and the
                                 size past which a tool result is swapped for a preview;
                                 part of the release bundle, so a change is a new bundle
estimate_tokens(text)            four characters a token, rounded up: no network call,
                                 the same on every machine (A-153 records where it errs)
fit(items, ceiling)              drops the lowest-priority items until the rest fit;
                                 a pinned item (instructions, a span a draft cites) is
                                 never dropped, and pinned items over the ceiling refuse

call type            starting ceiling   where it applies
tier2_extract        20,000             tier 2's one extraction over retrieved chunks
agent_first_call     15,000             an agent loop's first model call
agent_later_call     50,000             every later call of an agent run
copilot_turn         30,000             a Copilot turn
document_haiku       90,000             whole-document reads on Haiku 5.5 (6.5, not built)
document             200,000            whole-document reads on Sonnet 5.5 (6.5, not built)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ccp.config import REPO_ROOT

CEILINGS_FILE = REPO_ROOT / "config" / "context_ceilings.yaml"
CHARS_PER_TOKEN = 4


class ContextCeilingExceeded(RuntimeError):
    """What must be sent (instructions, cited spans) is already over the call's ceiling."""


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return -(-len(text) // CHARS_PER_TOKEN)


@dataclass(frozen=True)
class Ceilings:
    by_call: dict[str, int]
    swap_tokens: int
    preview_tokens: int

    def of(self, call_type: str) -> int:
        try:
            return self.by_call[call_type]
        except KeyError:
            raise KeyError(f"no ceiling for call type {call_type!r}") from None


@lru_cache(maxsize=4)
def load(path: Path = CEILINGS_FILE) -> Ceilings:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Ceilings(
        by_call={str(k): int(v) for k, v in (loaded.get("ceilings") or {}).items()},
        swap_tokens=int(loaded.get("tool_result_swap_tokens", 5000)),
        preview_tokens=int(loaded.get("preview_tokens", 500)),
    )


@dataclass(frozen=True)
class Item:
    key: str
    tokens: int
    priority: int  # lower is kept longer
    pinned: bool = False


def fit(items: list[Item], budget: int) -> tuple[list[str], list[str]]:
    """(kept keys in their given order, dropped keys): the lowest priority goes first, and
    among equals the later one. Pinned items stay; pinned items alone over budget refuse."""
    pinned = sum(i.tokens for i in items if i.pinned)
    if pinned > budget:
        raise ContextCeilingExceeded(f"pinned context of {pinned} tokens is over {budget}")
    total = sum(i.tokens for i in items)
    dropped: set[str] = set()
    order = sorted(
        (i for i in items if not i.pinned),
        key=lambda i: (i.priority, items.index(i)),
        reverse=True,
    )
    for item in order:
        if total <= budget:
            break
        dropped.add(item.key)
        total -= item.tokens
    return [i.key for i in items if i.key not in dropped], [
        i.key for i in items if i.key in dropped
    ]


def preview(text: str, tokens: int) -> str:
    """The start of a stored result, about `tokens` long."""
    return text[: tokens * CHARS_PER_TOKEN]
