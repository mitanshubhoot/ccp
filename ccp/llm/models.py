"""The models CCP may call, by exact id, and what each one accepts (spec.md A-103).

Pinned ids. A request names the exact snapshot, never an alias that can move. The 4.5
generation has aliases that resolve to a dated id (``claude-haiku-4-5`` ->
``claude-haiku-4-5-20251001``); those are refused with the dated id to use instead, as is
any ``-latest`` id. From Opus 4.6 on, the id itself is the snapshot: no dated form exists,
so it is pinned as written. An id this table does not know is refused before any call,
because the client could not tell whether it thinks by default or how to turn thinking off.

Thinking per call type (A-100). Reasoning calls (tier 3, the agents) run adaptive thinking
at medium effort. Every other call (tier 2, classification, the judge) turns thinking off
the way its model accepts:

    disabled        thinking {"type": "disabled"}   Haiku 5.5, Sonnet 5, Opus 5
    between_tools   thinking {"type": "between_tools"}   Sonnet 5.5, which rejects disabled
    omit            no thinking parameter            models that think only when asked
    never           cannot be turned off             Opus 5.5, the Fable and Mythos models:
                                                     low effort and room in max_tokens

Effort is sent explicitly wherever a model accepts it, so a model's default (medium on
Haiku 5.5 and Opus 5.5, high elsewhere) never changes a call silently. No sampling
parameter (temperature, top_p, top_k) is ever sent: API version 2026-10-21, which the
client pins, rejects them on every model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ccp.llm.errors import TerminalModelError

# Sent on every request, so an SDK upgrade cannot change the API version silently.
ANTHROPIC_API_VERSION = "2026-10-21"

ThinkingOff = Literal["disabled", "between_tools", "omit", "never"]
Effort = Literal["low", "medium", "high"]

REASONING_EFFORT: Effort = "medium"
PLAIN_EFFORT: Effort = "medium"  # thinking off: the answer itself at the default depth
ALWAYS_THINKS_EFFORT: Effort = "low"  # thinking cannot be off: think as little as allowed


@dataclass(frozen=True)
class ModelSpec:
    id: str
    adaptive: bool  # accepts thinking {"type": "adaptive"}
    thinks_by_default: bool  # an omitted `thinking` runs adaptive thinking
    off: ThinkingOff  # how a call without reasoning turns thinking off
    effort: bool  # accepts output_config.effort


MODELS: dict[str, ModelSpec] = {
    spec.id: spec
    for spec in (
        ModelSpec("claude-haiku-5-5", True, True, "disabled", True),
        ModelSpec("claude-sonnet-5-5", True, True, "between_tools", True),
        ModelSpec("claude-sonnet-5", True, True, "disabled", True),
        ModelSpec("claude-opus-5-5", True, True, "never", True),
        ModelSpec("claude-opus-5", True, True, "disabled", True),
        ModelSpec("claude-fable-5-1", True, True, "never", True),
        ModelSpec("claude-haiku-4-5-20251001", False, False, "omit", False),
    )
}

# Aliases that resolve to a dated snapshot and can move to a newer one.
ALIASES: dict[str, str] = {
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
    "claude-opus-4-5": "claude-opus-4-5-20251101",
}

_FAMILIES: tuple[tuple[str, str], ...] = (
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("gemini-", "google"),
)
_STUB = re.compile(r"^stub:")


class UnpinnedModel(TerminalModelError):
    """An alias that can move to another snapshot, or an id this table does not know:
    refused before any call, like a model with no price (A-97)."""


def family(model: str) -> str:
    """The vendor family of a model id; a stub's id is its stand-in's family."""
    name = _STUB.sub("", model.strip().lower())
    for prefix, vendor in _FAMILIES:
        if name.startswith(prefix):
            return vendor
    return "unknown"


def spec_for(model: str) -> ModelSpec:
    """The pinned model's spec; an alias, a -latest id, or an unknown id is refused."""
    if model in ALIASES:
        raise UnpinnedModel(
            f"{model!r} is an alias that can move to a newer snapshot; pin {ALIASES[model]!r}"
        )
    if model.endswith("-latest"):
        raise UnpinnedModel(f"{model!r} floats; name the snapshot id instead")
    spec = MODELS.get(model)
    if spec is None:
        raise UnpinnedModel(
            f"model {model!r} is not in ccp/llm/models.py; add it with what it accepts"
            " (thinking, effort) and its price in ccp/observability/cost.py before use"
        )
    return spec


def sampling_params(model: str, *, reasoning: bool) -> dict[str, object]:
    """Thinking and effort for one call (A-100, A-103); never a sampling parameter."""
    spec = spec_for(model)
    params: dict[str, object] = {}
    effort: Effort = PLAIN_EFFORT
    if reasoning and spec.adaptive:
        params["thinking"] = {"type": "adaptive"}
        effort = REASONING_EFFORT
    elif not reasoning:
        if spec.off == "disabled":
            params["thinking"] = {"type": "disabled"}
        elif spec.off == "between_tools":
            params["thinking"] = {"type": "between_tools"}
        elif spec.off == "never":
            effort = ALWAYS_THINKS_EFFORT
    if spec.effort:
        params["output_config"] = {"effort": effort}
    return params


def thinks(model: str, params: dict[str, object]) -> bool:
    """Whether a call with these params can spend max_tokens on thinking: adaptive asked
    for, or nothing said on a model that thinks by default. between_tools thinks between
    tool calls, so a tool loop needs the room too."""
    thinking = params.get("thinking")
    kind = thinking.get("type") if isinstance(thinking, dict) else None
    if kind is None:
        return spec_for(model).thinks_by_default
    return bool(kind != "disabled")
