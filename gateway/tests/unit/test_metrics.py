"""Metrics assembly: percentile math, buckets, cost, and the service snapshot."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from gateway.adapters.memory import InMemoryJobRepository
from gateway.core.metrics import (
    THROUGHPUT_HOURS,
    CompletedTiming,
    HourBucket,
    LatencyStats,
    estimated_cost_usd,
    execution_seconds,
    hour_buckets,
    hour_floor,
    latency_stats,
    nearest_rank,
    window_span,
)
from gateway.core.models import ErrorCode, Job, JobResult, JobStatus
from gateway.core.service import JobService
from tests.conftest import (
    FROZEN,
    FakeGuardrail,
    FakeRunPodClient,
    FrozenClock,
    make_job,
)

# --- nearest-rank percentiles ---


@pytest.mark.parametrize(
    ("percentile", "expected"),
    [(50, 5.0), (95, 10.0), (100, 10.0), (1, 1.0)],
)
def test_nearest_rank_on_one_to_ten(percentile: float, expected: float) -> None:
    values = [float(n) for n in range(1, 11)]

    assert nearest_rank(values, percentile) == expected


def test_nearest_rank_on_a_single_value() -> None:
    assert nearest_rank([7.0], 50) == 7.0
    assert nearest_rank([7.0], 95) == 7.0


def test_nearest_rank_on_an_odd_length_series() -> None:
    assert nearest_rank([1.0, 2.0, 3.0], 50) == 2.0
    assert nearest_rank([1.0, 2.0, 3.0], 95) == 3.0


def test_latency_stats_sorts_before_ranking() -> None:
    stats = latency_stats([30.0, 10.0, 20.0])

    assert stats == LatencyStats(p50_s=20.0, p95_s=30.0, max_s=30.0)


def test_latency_stats_on_an_empty_series_is_all_none() -> None:
    assert latency_stats([]) == LatencyStats(p50_s=None, p95_s=None, max_s=None)


# --- hourly buckets ---


def test_hour_buckets_are_dense_zero_filled_and_end_at_the_current_hour() -> None:
    noon = hour_floor(FROZEN)
    counts = {noon: 2, noon - timedelta(hours=3): 1}

    buckets = hour_buckets(counts, FROZEN)

    assert len(buckets) == THROUGHPUT_HOURS
    assert buckets[0].hour == noon - timedelta(hours=23)
    assert buckets[-1] == HourBucket(hour=noon, completed=2)
    assert buckets[20] == HourBucket(hour=noon - timedelta(hours=3), completed=1)
    assert sum(bucket.completed for bucket in buckets) == 3


def test_hour_buckets_with_no_completions_are_all_zero() -> None:
    buckets = hour_buckets({}, FROZEN)

    assert [bucket.completed for bucket in buckets] == [0] * THROUGHPUT_HOURS


def test_hour_buckets_ignore_counts_outside_the_series() -> None:
    stale = {hour_floor(FROZEN) - timedelta(hours=24): 9}

    assert sum(bucket.completed for bucket in hour_buckets(stale, FROZEN)) == 0


# --- cost ---


def test_one_gpu_hour_costs_the_hourly_rate() -> None:
    assert estimated_cost_usd(3600.0, 1.75) == 1.75


def test_cost_scales_linearly_with_execution_seconds() -> None:
    assert estimated_cost_usd(100.0, 1.75) == pytest.approx(100 * 1.75 / 3600)


def test_zero_execution_seconds_costs_nothing() -> None:
    assert estimated_cost_usd(0.0, 1.75) == 0.0


# --- window execution seconds and span ---


def timing(minutes_ago: int, inference_s: float) -> CompletedTiming:
    done = FROZEN - timedelta(minutes=minutes_ago)
    return CompletedTiming(
        created_at=done - timedelta(seconds=inference_s),
        completed_at=done,
        inference_seconds=inference_s,
    )


def test_execution_seconds_sums_the_window() -> None:
    assert execution_seconds([timing(5, 10.0), timing(9, 20.5)]) == 30.5


def test_execution_seconds_of_an_empty_window_is_zero() -> None:
    assert execution_seconds([]) == 0.0


def test_window_span_is_the_oldest_and_newest_completion() -> None:
    started, ended = window_span([timing(5, 10.0), timing(90, 20.0), timing(30, 5.0)])

    assert started == FROZEN - timedelta(minutes=90)
    assert ended == FROZEN - timedelta(minutes=5)


def test_window_span_of_one_completion_is_a_single_instant() -> None:
    started, ended = window_span([timing(7, 10.0)])

    assert started == ended == FROZEN - timedelta(minutes=7)


def test_window_span_of_an_empty_window_is_none() -> None:
    assert window_span([]) == (None, None)


# --- service assembly ---


def result_with(inference_s: float) -> JobResult:
    return JobResult(
        image_base64="aGVsbG8=",
        format="png",
        seed=42,
        width=1024,
        height=1024,
        model_version="black-forest-labs/FLUX.1-dev@0ef5fff",
        inference_seconds=inference_s,
    )


def job_created_at(created: datetime, api_key_id: str = "demo") -> Job:
    return make_job(api_key_id=api_key_id).advanced(
        created_at=created, updated_at=created
    )


async def complete(
    repository: InMemoryJobRepository,
    clock: FrozenClock,
    job: Job,
    done: datetime,
    inference_s: float,
) -> None:
    clock.at = done
    await repository.mark_completed(job.id, result_with(inference_s))


async def seeded(repository: InMemoryJobRepository, clock: FrozenClock) -> None:
    """Three demo completions, one queued, one failed, one other-caller job.

    All rows are created before any completes: the memory adapter evicts
    terminal rows past retention on `create`, and a completion older than an
    hour must not vanish mid-arrange.
    """
    await repository.create(job_created_at(FROZEN - timedelta(minutes=1)))
    failed = await repository.create(job_created_at(FROZEN - timedelta(minutes=90)))
    slow = await repository.create(job_created_at(FROZEN - timedelta(hours=2)))
    mid = await repository.create(job_created_at(FROZEN - timedelta(minutes=30)))
    fresh = await repository.create(job_created_at(FROZEN - timedelta(minutes=10)))
    other = await repository.create(
        job_created_at(FROZEN - timedelta(minutes=20), api_key_id="other")
    )
    for job in (failed, slow, mid, fresh, other):
        await repository.attach_runpod_id(job.id, f"up-{job.id}")
    await complete(
        repository, clock, slow, done=FROZEN - timedelta(minutes=110), inference_s=20.0
    )
    await complete(
        repository, clock, mid, done=FROZEN - timedelta(minutes=25), inference_s=10.0
    )
    await complete(
        repository, clock, fresh, done=FROZEN - timedelta(minutes=5), inference_s=30.0
    )
    await complete(
        repository, clock, other, done=FROZEN - timedelta(minutes=15), inference_s=99.0
    )
    await repository.mark_failed(
        failed.id, ErrorCode.INFERENCE_FAILED, "boom", JobStatus.FAILED
    )
    clock.at = FROZEN


def make_service(
    repository: InMemoryJobRepository, clock: FrozenClock, window: int = 100
) -> JobService:
    return JobService(
        repository=repository,
        runpod=FakeRunPodClient(),
        guardrail=FakeGuardrail(),
        clock=clock,
        metrics_window=window,
    )


async def test_metrics_counts_are_caller_scoped_and_zero_filled(
    repository: InMemoryJobRepository, clock: FrozenClock
) -> None:
    await seeded(repository, clock)

    snapshot = await make_service(repository, clock).metrics("demo")

    assert snapshot.by_status[JobStatus.COMPLETED] == 3
    assert snapshot.by_status[JobStatus.QUEUED] == 1
    assert snapshot.by_status[JobStatus.FAILED] == 1
    assert snapshot.by_status[JobStatus.CANCELLED] == 0
    assert snapshot.active_now == 1
    # The queued job plus the two completions created 30 and 10 minutes ago.
    assert snapshot.created_last_hour == 3
    assert snapshot.generated_at == FROZEN


async def test_metrics_percentiles_cover_both_series(
    repository: InMemoryJobRepository, clock: FrozenClock
) -> None:
    await seeded(repository, clock)

    snapshot = await make_service(repository, clock).metrics("demo")

    # inference: [10, 20, 30]; wall: [300, 300, 600] once sorted.
    assert snapshot.inference == LatencyStats(p50_s=20.0, p95_s=30.0, max_s=30.0)
    assert snapshot.wall == LatencyStats(p50_s=300.0, p95_s=600.0, max_s=600.0)
    assert snapshot.completed_in_window == 3
    assert snapshot.window == 100


async def test_metrics_cost_prices_the_windows_execution_seconds(
    repository: InMemoryJobRepository, clock: FrozenClock
) -> None:
    await seeded(repository, clock)

    snapshot = await make_service(repository, clock).metrics("demo")

    assert snapshot.estimated_cost_usd == pytest.approx(60 * 1.75 / 3600)
    assert snapshot.estimated_cost_usd_per_job == pytest.approx(20 * 1.75 / 3600)
    assert snapshot.gpu_rate_usd_hr == 1.75


async def test_metrics_cost_is_recomputable_from_the_seconds_it_reports(
    repository: InMemoryJobRepository, clock: FrozenClock
) -> None:
    """The estimate carries its own input, so a reader can check it."""
    await seeded(repository, clock)

    snapshot = await make_service(repository, clock).metrics("demo")

    assert snapshot.exec_seconds_in_window == 60.0
    assert snapshot.estimated_cost_usd == pytest.approx(
        snapshot.exec_seconds_in_window * snapshot.gpu_rate_usd_hr / 3600
    )


async def test_metrics_report_the_windows_completion_span(
    repository: InMemoryJobRepository, clock: FrozenClock
) -> None:
    await seeded(repository, clock)

    snapshot = await make_service(repository, clock).metrics("demo")

    assert snapshot.window_started_at == FROZEN - timedelta(minutes=110)
    assert snapshot.window_ended_at == FROZEN - timedelta(minutes=5)


async def test_a_narrower_window_narrows_the_span_and_the_seconds(
    repository: InMemoryJobRepository, clock: FrozenClock
) -> None:
    await seeded(repository, clock)

    snapshot = await make_service(repository, clock, window=2).metrics("demo")

    assert snapshot.exec_seconds_in_window == 40.0
    assert snapshot.window_started_at == FROZEN - timedelta(minutes=25)
    assert snapshot.window_ended_at == FROZEN - timedelta(minutes=5)


async def test_metrics_throughput_buckets_the_last_24_hours(
    repository: InMemoryJobRepository, clock: FrozenClock
) -> None:
    await seeded(repository, clock)

    snapshot = await make_service(repository, clock).metrics("demo")

    by_hour = {bucket.hour: bucket.completed for bucket in snapshot.throughput}
    assert len(snapshot.throughput) == 24
    assert by_hour[hour_floor(FROZEN) - timedelta(hours=2)] == 1
    assert by_hour[hour_floor(FROZEN) - timedelta(hours=1)] == 2
    assert sum(by_hour.values()) == 3


async def test_metrics_window_keeps_only_the_newest_completions(
    repository: InMemoryJobRepository, clock: FrozenClock
) -> None:
    await seeded(repository, clock)

    snapshot = await make_service(repository, clock, window=2).metrics("demo")

    # Newest two by completed_at: inference [30, 10].
    assert snapshot.completed_in_window == 2
    assert snapshot.inference == LatencyStats(p50_s=10.0, p95_s=30.0, max_s=30.0)
    assert snapshot.estimated_cost_usd == pytest.approx(40 * 1.75 / 3600)
    assert snapshot.window == 2


async def test_metrics_on_an_empty_store_is_all_zeroes_and_nones(
    repository: InMemoryJobRepository, clock: FrozenClock
) -> None:
    snapshot = await make_service(repository, clock).metrics("demo")

    assert all(count == 0 for count in snapshot.by_status.values())
    assert snapshot.created_last_hour == 0
    assert snapshot.active_now == 0
    assert snapshot.inference == LatencyStats(p50_s=None, p95_s=None, max_s=None)
    assert snapshot.wall == LatencyStats(p50_s=None, p95_s=None, max_s=None)
    assert snapshot.completed_in_window == 0
    assert snapshot.estimated_cost_usd == 0.0
    assert snapshot.estimated_cost_usd_per_job is None
    assert snapshot.exec_seconds_in_window == 0.0
    assert snapshot.window_started_at is None
    assert snapshot.window_ended_at is None
    assert [bucket.completed for bucket in snapshot.throughput] == [0] * 24


async def test_endpoint_health_age_tracks_the_clock(
    repository: InMemoryJobRepository, clock: FrozenClock
) -> None:
    service = make_service(repository, clock)
    assert service.endpoint_health_age_s is None

    await service.reconcile()
    clock.advance(7)

    assert service.endpoint_health_age_s == 7.0
