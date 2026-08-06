"""The background loop: cadence, survival, and clean shutdown.

The loop is the only thing that ever learns a job's outcome, so the failure
that matters is not a wrong result — it is the loop quietly stopping while the
process stays perfectly alive. These drive it against a stub service and a
recording `asyncio.sleep`, so a tick is a step rather than a wall-clock wait.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
import structlog.testing

from gateway.workers import reconciler as reconciler_module
from gateway.workers.reconciler import JITTER, Reconciler

INTERVAL_S = 2.0
IDLE_INTERVAL_S = 10.0

# Captured before the `sleeps` fixture replaces `asyncio.sleep`, so a test that
# needs to actually wait is not itself stopped by the loop-stopping fake.
_REAL_SLEEP = asyncio.sleep


@dataclass
class StubService:
    """Only what `Reconciler` touches: `reconcile()` and `outstanding`."""

    advanced: int = 0
    outstanding: int = 0
    raises: list[BaseException | None] = field(default_factory=list)
    batches: list[int] = field(default_factory=list)

    async def reconcile(self, limit: int = 50) -> int:
        self.batches.append(limit)
        if self.raises:
            failure = self.raises.pop(0)
            if failure is not None:
                raise failure
        return self.advanced


def make(service: StubService, batch: int = 50) -> Reconciler:
    return Reconciler(
        service=service,  # type: ignore[arg-type]
        interval_s=INTERVAL_S,
        idle_interval_s=IDLE_INTERVAL_S,
        batch=batch,
    )


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record each sleep and stop the loop after the third, without waiting."""
    recorded: list[float] = []
    real_sleep = _REAL_SLEEP

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)
        if len(recorded) >= 3:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(reconciler_module.asyncio, "sleep", fake_sleep)
    return recorded


async def run_until_stopped(reconciler: Reconciler) -> None:
    with pytest.raises(asyncio.CancelledError):
        await reconciler.run_forever()


def within_jitter(delay: float, base: float) -> bool:
    return base * (1 - JITTER) <= delay <= base * (1 + JITTER)


async def test_outstanding_work_holds_the_fast_cadence(sleeps: list[float]) -> None:
    await run_until_stopped(make(StubService(advanced=0, outstanding=4)))

    assert len(sleeps) == 3
    assert all(within_jitter(delay, INTERVAL_S) for delay in sleeps)


async def test_an_idle_tick_backs_off_to_the_idle_interval(sleeps: list[float]) -> None:
    await run_until_stopped(make(StubService(advanced=0, outstanding=0)))

    assert all(within_jitter(delay, IDLE_INTERVAL_S) for delay in sleeps)


async def test_a_tick_that_advanced_work_keeps_the_fast_cadence(
    sleeps: list[float],
) -> None:
    """A job that just resolved deserves a 2s follow-up, not a 10s one."""
    await run_until_stopped(make(StubService(advanced=2, outstanding=0)))

    assert all(within_jitter(delay, INTERVAL_S) for delay in sleeps)


async def test_a_running_job_reporting_nothing_still_gets_the_fast_cadence(
    sleeps: list[float],
) -> None:
    await run_until_stopped(make(StubService(advanced=0, outstanding=1)))

    assert all(within_jitter(delay, INTERVAL_S) for delay in sleeps)


async def test_the_loop_survives_a_failing_tick(sleeps: list[float]) -> None:
    """One bad tick must not end polling for every job in the system."""
    service = StubService(raises=[RuntimeError("upstream exploded")])

    await run_until_stopped(make(service))

    assert len(service.batches) == 3


async def test_a_failing_tick_is_treated_as_idle(sleeps: list[float]) -> None:
    """`advanced` never got assigned, so the backoff must not read a stale one."""
    service = StubService(advanced=5, outstanding=0, raises=[RuntimeError("boom")])

    await run_until_stopped(make(service))

    assert within_jitter(sleeps[0], IDLE_INTERVAL_S)


async def test_a_cancelled_tick_is_not_swallowed_as_a_tick_failure(
    sleeps: list[float],
) -> None:
    """Shutdown must stop the loop, not be logged as one more failed tick."""
    service = StubService(raises=[asyncio.CancelledError()])

    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(asyncio.CancelledError),
    ):
        await make(service).run_forever()

    assert sleeps == []
    assert [entry for entry in logs if entry["event"] == "reconcile_tick_failed"] == []


async def test_a_failing_tick_is_logged(sleeps: list[float]) -> None:
    service = StubService(raises=[RuntimeError("upstream exploded")])

    with structlog.testing.capture_logs() as logs:
        await run_until_stopped(make(service))

    failures = [entry for entry in logs if entry["event"] == "reconcile_tick_failed"]
    assert len(failures) == 1
    assert failures[0]["error"] == "upstream exploded"


async def test_the_batch_size_is_passed_to_the_service(sleeps: list[float]) -> None:
    service = StubService()

    await run_until_stopped(make(service, batch=7))

    assert service.batches == [7, 7, 7]


async def test_seconds_since_last_run_is_none_before_the_first_tick() -> None:
    assert make(StubService()).seconds_since_last_run is None


async def test_seconds_since_last_run_is_set_once_a_tick_completes(
    sleeps: list[float],
) -> None:
    reconciler = make(StubService())

    await run_until_stopped(reconciler)

    age = reconciler.seconds_since_last_run
    assert age is not None
    assert age >= 0.0


async def test_seconds_since_last_run_grows_with_wall_time(
    sleeps: list[float],
) -> None:
    """A frozen age would make a dead loop indistinguishable from a live one."""
    reconciler = make(StubService())
    await run_until_stopped(reconciler)

    first = reconciler.seconds_since_last_run
    await _REAL_SLEEP(0.01)
    second = reconciler.seconds_since_last_run

    assert first is not None
    assert second is not None
    assert second > first


async def test_a_failed_tick_still_counts_as_a_tick(sleeps: list[float]) -> None:
    """Otherwise a loop that is alive but always failing reports as stalled."""
    reconciler = make(StubService(raises=[RuntimeError("boom")]))

    await run_until_stopped(reconciler)

    assert reconciler.seconds_since_last_run is not None


async def test_running_starts_the_loop_and_stops_it_on_exit() -> None:
    service = StubService()
    reconciler = make(service)

    async with reconciler.running():
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert service.batches
    assert reconciler._task is None


async def test_running_awaits_the_in_flight_tick_rather_than_killing_it() -> None:
    """A tick cut off mid-write leaves a job in a state nothing will revisit."""
    finished = asyncio.Event()

    @dataclass
    class SlowService:
        outstanding: int = 0

        async def reconcile(self, limit: int = 50) -> int:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                finished.set()
                raise
            return 0

    reconciler = Reconciler(service=SlowService())  # type: ignore[arg-type]
    async with reconciler.running():
        await asyncio.sleep(0)

    assert finished.is_set()
    assert reconciler._task is None


async def test_running_is_reentrant_after_a_clean_exit() -> None:
    service = StubService()
    reconciler = make(service)

    async with reconciler.running():
        await asyncio.sleep(0)
    async with reconciler.running():
        await asyncio.sleep(0)

    assert reconciler._task is None
