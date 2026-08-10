"""Token bucket: burst, refill, exhaustion, Retry-After arithmetic."""

from __future__ import annotations

from gateway.adapters.ratelimit import TokenBucketRateLimiter
from tests.conftest import FrozenClock

# rpm=30 refills at 0.5 tokens/s: every arithmetic assertion below leans on it.
RPM = 30


def limiter(
    clock: FrozenClock, rpm: int = RPM, burst: int = 10
) -> TokenBucketRateLimiter:
    return TokenBucketRateLimiter(clock=clock, rpm=rpm, burst=burst)


def test_a_full_bucket_admits_exactly_the_burst(clock: FrozenClock) -> None:
    bucket = limiter(clock, burst=10)

    decisions = [bucket.acquire("demo") for _ in range(11)]

    assert all(d.allowed for d in decisions[:10])
    assert not decisions[10].allowed


def test_refill_restores_a_token_at_the_rpm_rate(clock: FrozenClock) -> None:
    bucket = limiter(clock, burst=1)
    bucket.acquire("demo")
    assert not bucket.acquire("demo").allowed

    clock.advance(2)  # 0.5 tokens/s * 2s = 1 token

    assert bucket.acquire("demo").allowed


def test_a_partial_refill_is_still_denied(clock: FrozenClock) -> None:
    bucket = limiter(clock, burst=1)
    bucket.acquire("demo")

    clock.advance(1)  # 0.5 tokens: not enough

    assert not bucket.acquire("demo").allowed


def test_retry_after_is_the_ceiled_wait_for_one_token(clock: FrozenClock) -> None:
    bucket = limiter(clock, burst=1)
    bucket.acquire("demo")

    empty = bucket.acquire("demo")
    clock.advance(1)  # 0.5 tokens left to earn: 1s at 0.5/s
    half_full = bucket.acquire("demo")

    assert empty.retry_after_s == 2
    assert half_full.retry_after_s == 1


def test_retry_after_never_reports_zero(clock: FrozenClock) -> None:
    bucket = limiter(clock, rpm=600, burst=1)  # 10 tokens/s: sub-second waits
    bucket.acquire("demo")

    denied = bucket.acquire("demo")

    assert not denied.allowed
    assert denied.retry_after_s == 1


def test_an_allowed_call_reports_no_wait(clock: FrozenClock) -> None:
    assert limiter(clock).acquire("demo").retry_after_s == 0


def test_buckets_are_independent_per_key(clock: FrozenClock) -> None:
    bucket = limiter(clock, burst=1)
    bucket.acquire("demo")
    assert not bucket.acquire("demo").allowed

    assert bucket.acquire("other").allowed


def test_an_idle_bucket_never_refills_past_the_burst(clock: FrozenClock) -> None:
    bucket = limiter(clock, burst=2)
    bucket.acquire("demo")
    bucket.acquire("demo")

    clock.advance(3600)
    decisions = [bucket.acquire("demo") for _ in range(3)]

    assert [d.allowed for d in decisions] == [True, True, False]


def test_a_denied_call_consumes_nothing(clock: FrozenClock) -> None:
    """Denials must not reset refill progress, or a poller could starve forever."""
    bucket = limiter(clock, burst=1)
    bucket.acquire("demo")

    clock.advance(1)  # 0.5 tokens accrued
    assert not bucket.acquire("demo").allowed
    clock.advance(1)  # the other 0.5: progress kept across the denial

    assert bucket.acquire("demo").allowed
