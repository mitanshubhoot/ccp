"""RunBudget: hard caps enforced inside the client, so no graph can exceed them (spec.md A-11).

    budget = RunBudget.from_settings(settings)
    tracker = BudgetTracker(budget)
    tracker.charge(input_tokens=..., output_tokens=..., usd=...)  # BudgetExceeded past a cap
    tracker.tool_call()                                              # counts tool calls
    tracker.check_clock()                                            # wall-clock cap

The clock is injectable so the wall-clock cap is testable without sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ccp.config import Settings
from ccp.llm.errors import BudgetExceeded


@dataclass(frozen=True)
class RunBudget:
    max_tokens: int
    max_usd: float
    max_seconds: float
    max_tool_calls: int = 8

    @classmethod
    def from_settings(cls, settings: Settings) -> RunBudget:
        return cls(
            max_tokens=settings.ccp_run_budget_tokens,
            max_usd=settings.ccp_run_budget_usd,
            max_seconds=float(settings.ccp_run_budget_seconds),
        )

    def lowered(
        self,
        *,
        max_tokens: int | None = None,
        max_usd: float | None = None,
        max_seconds: float | None = None,
        max_tool_calls: int | None = None,
    ) -> RunBudget:
        """A graph may lower a cap, never raise it."""
        return RunBudget(
            max_tokens=min(
                self.max_tokens, max_tokens if max_tokens is not None else self.max_tokens
            ),
            max_usd=min(self.max_usd, max_usd if max_usd is not None else self.max_usd),
            max_seconds=min(
                self.max_seconds, max_seconds if max_seconds is not None else self.max_seconds
            ),
            max_tool_calls=min(
                self.max_tool_calls,
                max_tool_calls if max_tool_calls is not None else self.max_tool_calls,
            ),
        )


@dataclass
class BudgetTracker:
    budget: RunBudget
    clock: Callable[[], float] = time.monotonic
    tokens: int = 0  # every token of every call, for reporting
    peak_context: int = 0  # the largest single call: what the token cap bounds
    usd: float = 0.0
    tool_calls: int = 0
    started: float = field(default=0.0)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def charge(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        usd: float,
        context_tokens: int | None = None,
    ) -> None:
        """Count one call (A-100). The token cap bounds the context of a single call,
        cached or not (input, cache reads and writes, and output); what a run may spend
        in total is bounded by the dollar cap, which prices every token class."""
        context = context_tokens if context_tokens is not None else input_tokens + output_tokens
        self.tokens += context
        self.peak_context = max(self.peak_context, context)
        self.usd += usd
        if context > self.budget.max_tokens:
            raise BudgetExceeded(
                f"budget_exceeded: token cap {self.budget.max_tokens} exceeded by one"
                f" call's context ({context})"
            )
        if self.usd > self.budget.max_usd:
            raise BudgetExceeded(
                f"budget_exceeded: usd cap {self.budget.max_usd:.2f} exceeded ({self.usd:.4f})"
            )
        self.check_clock()

    def tool_call(self) -> None:
        self.tool_calls += 1
        if self.tool_calls > self.budget.max_tool_calls:
            raise BudgetExceeded(
                f"budget_exceeded: tool-call cap {self.budget.max_tool_calls} exceeded"
            )
        self.check_clock()

    def check_clock(self) -> None:
        elapsed = self.clock() - self.started
        if elapsed > self.budget.max_seconds:
            raise BudgetExceeded(
                f"budget_exceeded: wall-clock cap {self.budget.max_seconds}s exceeded"
                f" ({elapsed:.1f}s)"
            )

    def snapshot(self) -> dict[str, float | int]:
        return {
            "tokens": self.tokens,
            "peak_context": self.peak_context,
            "usd": round(self.usd, 6),
            "tool_calls": self.tool_calls,
            "elapsed_s": round(self.clock() - self.started, 3),
        }
