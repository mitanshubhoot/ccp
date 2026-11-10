"""Error taxonomy for the model layer (spec.md D9, A-50, A-97).

    throttled  429, 529, 503                       -> wait with backoff; the try is not counted
    retryable  408, 409, 425, 5xx, connection errors, timeouts -> backoff, then the queue's attempts
    terminal   400, 401, 403, 404, schema refusal after one re-ask, budget exceeded, a model
               with no price (its spend could not be capped)            -> dead-letter
An infrastructure failure never becomes a verdict.
"""

from __future__ import annotations

from ccp.ingest.consumers import RetryableError, TerminalError, ThrottledError


class RetryableModelError(RetryableError):
    """Transient model-layer failure: retry with backoff."""


class ThrottledModelError(RetryableModelError, ThrottledError):
    """The provider is rate-limiting or overloaded: wait, without spending an attempt."""


class TerminalModelError(TerminalError):
    """Permanent model-layer failure: dead-letter on first occurrence."""


class BudgetExceeded(TerminalModelError):
    """A run budget cap was hit; the run stops and the record dead-letters with the cap named."""


class UnpricedModel(TerminalModelError):
    """No price is known for the model, so a dollar cap could not stop it; refused (A-97)."""


THROTTLE_STATUSES = frozenset({429, 503, 529})
RETRYABLE_STATUSES = frozenset({408, 409, 425, 500, 502, 504}) | THROTTLE_STATUSES


def classify_status(status_code: int, detail: str = "") -> RetryableModelError | TerminalModelError:
    message = f"HTTP {status_code}: {detail}".strip()
    if status_code in THROTTLE_STATUSES:
        return ThrottledModelError(message)
    if status_code in RETRYABLE_STATUSES:
        return RetryableModelError(message)
    return TerminalModelError(message)
