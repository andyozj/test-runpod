"""In-process token-bucket rate limiter, one bucket per api key."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from gateway.core.protocols import Clock, RateLimitDecision

DEFAULT_RATE_LIMIT_RPM = 30
DEFAULT_RATE_LIMIT_BURST = 10

SECONDS_PER_MINUTE = 60.0


@dataclass
class _Bucket:
    """One caller's remaining tokens and when they were last recomputed."""

    tokens: float
    updated_at: datetime


@dataclass
class TokenBucketRateLimiter:
    """`burst` tokens per key, refilled continuously at `rpm` per minute.

    In-process, like the queue-health cache: buckets live in this instance
    only, so N instances each admit their own `rpm` per key. Bucket count is
    bounded by the configured credential set — callers are authenticated
    before the limiter is consulted — so the dict cannot grow past it.

    Synchronous and lock-free: `acquire` never awaits, so under CPython's
    cooperative scheduler a read-modify-write on one bucket cannot interleave
    with another. The same reasoning as `InMemoryJobRepository.count_active`;
    a shared (e.g. Redis-backed) implementation would need its own atomicity.

    Attributes:
        clock: Injected wall time, so refill is assertable under a frozen
            clock.
        rpm: Sustained requests per minute per key; the refill rate.
        burst: Bucket capacity: requests admitted at once from a full bucket.
    """

    clock: Clock
    rpm: int = DEFAULT_RATE_LIMIT_RPM
    burst: int = DEFAULT_RATE_LIMIT_BURST
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def acquire(self, api_key_id: str) -> RateLimitDecision:
        """Spend one token, or report how long until one exists.

        A new key starts with a full bucket. A denied call consumes nothing,
        but the refill computed for it is kept, so repeated denials still
        accrue progress toward the next token.

        Args:
            api_key_id: The caller's identity; each caller has its own bucket.

        Returns:
            Allowed with `retry_after_s` zero, or denied with the ceiled
            seconds until a token is available, floored at one.
        """
        now = self.clock.now()
        rate = self.rpm / SECONDS_PER_MINUTE
        bucket = self._buckets.get(api_key_id)
        if bucket is None:
            bucket = _Bucket(tokens=float(self.burst), updated_at=now)
            self._buckets[api_key_id] = bucket
        else:
            elapsed = (now - bucket.updated_at).total_seconds()
            bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * rate)
            bucket.updated_at = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return RateLimitDecision(allowed=True, retry_after_s=0)
        return RateLimitDecision(
            allowed=False,
            retry_after_s=max(1, math.ceil((1.0 - bucket.tokens) / rate)),
        )
