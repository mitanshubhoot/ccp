"""Error taxonomy for the model layer (spec.md D9, A-50).

    retryable  429, 529, 5xx, connection errors, timeouts   -> backoff, then the queue's attempts
    terminal   400, 401, 403, 404, schema refusal after one re-ask, budget exceeded -> dead-letter
An infrastructure failure never becomes a verdict.
"""

from __future__ import annotations

from ccp.ingest.consumers import RetryableError, TerminalError


class RetryableModelError(RetryableError):
    """Transient model-layer failure: retry with backoff."""


class TerminalModelError(TerminalError):
    """Permanent model-layer failure: dead-letter on first occurrence."""


class BudgetExceeded(TerminalModelError):
    """A run budget cap was hit; the run stops and the record dead-letters with the cap named."""


RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


def classify_status(status_code: int, detail: str = "") -> RetryableModelError | TerminalModelError:
    if status_code in RETRYABLE_STATUSES:
        return RetryableModelError(f"HTTP {status_code}: {detail}".strip())
    return TerminalModelError(f"HTTP {status_code}: {detail}".strip())
