"""Rate buckets, one per (tenant, connection) (spec.md A-120).

Every request a pull makes spends a token from its connection's bucket first. The bucket
refills at a steady rate up to a burst, so one tenant's pulls share one budget: a scheduled
pull and a re-pull after a remediation cannot together outrun the provider's limits, and
another tenant's pulls never spend this tenant's budget. When the provider reports its
remaining quota (GitHub's x-ratelimit-remaining and x-ratelimit-reset), the bucket holds
until the reset once only a reserve is left, instead of spending the quota down to a refusal.

A taker reserves its token under the lock and sleeps outside it, so concurrent pulls on one
connection queue in order without holding the lock while they wait. Buckets live in the
process; a deployment with several workers divides the rate between them.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RateBucket:
    def __init__(
        self,
        rate: float,
        burst: int,
        reserve: int = 0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("a bucket needs a positive rate and a burst of at least one")
        self.rate = rate
        self.burst = float(burst)
        self.reserve = reserve
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._updated = clock()
        self._hold_until = 0.0
        self._lock = threading.Lock()
        self.waited = 0.0  # seconds every taker has spent waiting, for the run's record

    def take(self, sleep: Callable[[float], None] | None = None) -> float:
        """Spend one token, waiting first if there is none or the bucket is held. Returns
        the seconds waited."""
        with self._lock:
            now = self._clock()
            self._tokens = min(self.burst, self._tokens + (now - self._updated) * self.rate)
            self._updated = now
            self._tokens -= 1.0  # reserved now; a debt is paid by waiting
            wait = max(self._hold_until - now, -self._tokens / self.rate, 0.0)
            self.waited += wait
        if wait > 0:
            (sleep or self._sleep)(wait)
        return wait

    def observe(self, remaining: int | None, reset_in: float | None) -> None:
        """What the provider says is left: at or under the reserve, hold until the reset."""
        if remaining is None or reset_in is None or remaining > self.reserve:
            return
        with self._lock:
            self._hold_until = max(self._hold_until, self._clock() + max(0.0, reset_in))


_BUCKETS: dict[tuple[str, str], RateBucket] = {}
_REGISTRY_LOCK = threading.Lock()


def bucket_for(tenant_id: str, connection: str, make: Callable[[], RateBucket]) -> RateBucket:
    """The one bucket for this tenant's connection, made on first use."""
    with _REGISTRY_LOCK:
        bucket = _BUCKETS.get((tenant_id, connection))
        if bucket is None:
            bucket = _BUCKETS[(tenant_id, connection)] = make()
        return bucket


def reset() -> None:
    """Forget every bucket (tests, and a process that reloads its connections)."""
    with _REGISTRY_LOCK:
        _BUCKETS.clear()
