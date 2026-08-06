"""Circuit breaker state machine."""

from __future__ import annotations

from gateway.adapters.runpod_client import CircuitBreaker


def test_closed_breaker_allows() -> None:
    assert CircuitBreaker().allow(now=0.0)


def test_opens_after_the_threshold() -> None:
    breaker = CircuitBreaker(threshold=3, cooldown_s=30)

    for _ in range(3):
        breaker.record_failure(now=0.0)

    assert not breaker.allow(now=1.0)


def test_half_opens_after_the_cooldown() -> None:
    breaker = CircuitBreaker(threshold=1, cooldown_s=10)
    breaker.record_failure(now=0.0)

    assert not breaker.allow(now=5.0)
    assert breaker.allow(now=10.0)


def test_success_closes_it() -> None:
    breaker = CircuitBreaker(threshold=1, cooldown_s=100)
    breaker.record_failure(now=0.0)
    breaker.record_success()

    assert breaker.allow(now=1.0)


def test_half_open_admits_exactly_one_probe() -> None:
    """Every caller passing at once would re-hammer a host that is still down."""
    breaker = CircuitBreaker(threshold=1, cooldown_s=10)
    breaker.record_failure(now=0.0)

    assert breaker.allow(now=10.0)
    assert not breaker.allow(now=10.0)
    assert not breaker.allow(now=11.0)


def test_a_successful_probe_reopens_the_gate() -> None:
    breaker = CircuitBreaker(threshold=1, cooldown_s=10)
    breaker.record_failure(now=0.0)

    assert breaker.allow(now=10.0)
    breaker.record_success()

    assert breaker.allow(now=10.0)


def test_failed_probe_rearms_the_cooldown() -> None:
    """A failure during half-open must re-open, not leave it closed forever."""
    breaker = CircuitBreaker(threshold=1, cooldown_s=10)
    breaker.record_failure(now=0.0)

    assert breaker.allow(now=10.0)  # half-open probe permitted
    breaker.record_failure(now=10.0)  # the probe failed

    assert not breaker.allow(now=15.0)
    assert breaker.allow(now=20.0)
