"""Metrics assembly: pure math over the job ledger, no I/O.

`JobService.metrics` gathers the inputs from the repository; everything here
is deterministic arithmetic on those inputs, testable without a store.

No `>>>` examples: the doctest gate runs a fixed module list that does not
include this file, and an unexecuted doctest is documentation that rots.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from gateway.core.models import JobStatus

DEFAULT_METRICS_WINDOW = 100

# BENCHMARKS.md, 48GB tier: $1.75/hr (measured 2026-08-05, re-verify before
# quoting). An estimate, not billing data — hence `estimated_cost_usd`.
DEFAULT_GPU_RATE_USD_HR = 1.75

THROUGHPUT_HOURS = 24
SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True)
class CompletedTiming:
    """Timings of one completed job, all a latency percentile needs.

    Attributes:
        created_at: When the gateway accepted the job.
        completed_at: When the result was recorded.
        inference_seconds: The worker's wall-clock generation time.
    """

    created_at: datetime
    completed_at: datetime
    inference_seconds: float

    @property
    def wall_seconds(self) -> float:
        """Caller-observed duration: completion minus creation.

        Returns:
            Seconds between `created_at` and `completed_at`.
        """
        return (self.completed_at - self.created_at).total_seconds()


@dataclass(frozen=True)
class LatencyStats:
    """Percentiles over one latency series.

    Attributes:
        p50_s: Median, None when the series is empty.
        p95_s: 95th percentile, None when the series is empty.
        max_s: Largest value, None when the series is empty.
    """

    p50_s: float | None
    p95_s: float | None
    max_s: float | None


@dataclass(frozen=True)
class HourBucket:
    """Completions in one UTC hour.

    Attributes:
        hour: Start of the hour, UTC.
        completed: Jobs completed within it.
    """

    hour: datetime
    completed: int


@dataclass(frozen=True)
class MetricsSnapshot:
    """One caller's dashboard aggregates, assembled at a single instant.

    Attributes:
        by_status: All-time job counts in the store, every status present.
        created_last_hour: Jobs created in the trailing hour.
        active_now: Non-terminal jobs right now.
        inference: Percentiles of `inference_seconds` over the window.
        wall: Percentiles of `completed_at - created_at` over the window.
        throughput: Completions per hour, oldest first, zeros included.
        window: Configured window size (jobs), not how many were found.
        completed_in_window: Completed jobs the window actually held.
        window_started_at: Oldest `completed_at` in the window, None when
            empty. With `window_ended_at` it gives the window a span, so the
            cost can be read as a rate rather than a total over unknown time.
        window_ended_at: Newest `completed_at` in the window, None when empty.
        estimated_cost_usd: Execution seconds in the window priced at
            `gpu_rate_usd_hr`. An estimate, not billing data.
        estimated_cost_usd_per_job: The above divided by the window's job
            count, None when the window is empty.
        exec_seconds_in_window: The execution seconds the cost priced. Carried
            so a reader can recompute the estimate rather than trust it.
        gpu_rate_usd_hr: The rate the estimate used.
        generated_at: When the snapshot was assembled.
    """

    by_status: dict[JobStatus, int]
    created_last_hour: int
    active_now: int
    inference: LatencyStats
    wall: LatencyStats
    throughput: list[HourBucket]
    window: int
    completed_in_window: int
    window_started_at: datetime | None
    window_ended_at: datetime | None
    estimated_cost_usd: float
    estimated_cost_usd_per_job: float | None
    exec_seconds_in_window: float
    gpu_rate_usd_hr: float
    generated_at: datetime


def nearest_rank(ordered: Sequence[float], percentile: float) -> float:
    """Nearest-rank percentile: the value at rank `ceil(p/100 * n)`, 1-based.

    Args:
        ordered: The series, sorted ascending, non-empty.
        percentile: Which percentile, 0-100.

    Returns:
        The element at the nearest rank.
    """
    rank = math.ceil(percentile / 100 * len(ordered))
    return ordered[max(rank, 1) - 1]


def latency_stats(seconds: Sequence[float]) -> LatencyStats:
    """Summarise one latency series by nearest-rank percentiles.

    Args:
        seconds: Durations in seconds, any order.

    Returns:
        p50/p95/max, or all-None for an empty series.
    """
    if not seconds:
        return LatencyStats(p50_s=None, p95_s=None, max_s=None)
    ordered = sorted(seconds)
    return LatencyStats(
        p50_s=nearest_rank(ordered, 50),
        p95_s=nearest_rank(ordered, 95),
        max_s=ordered[-1],
    )


def hour_floor(moment: datetime) -> datetime:
    """Truncate a timestamp to the start of its hour.

    Args:
        moment: An aware UTC datetime.

    Returns:
        The same instant with minutes and smaller zeroed.
    """
    return moment.replace(minute=0, second=0, microsecond=0)


def hour_buckets(
    counts: Mapping[datetime, int], now: datetime, hours: int = THROUGHPUT_HOURS
) -> list[HourBucket]:
    """Fill a sparse per-hour count into a dense, zero-padded series.

    Args:
        counts: Completions keyed by UTC hour start; hours with none absent.
        now: The current time; the series ends at its hour.
        hours: Series length.

    Returns:
        `hours` buckets, oldest first, ending with the current hour.
    """
    start = hour_floor(now) - timedelta(hours=hours - 1)
    return [
        HourBucket(hour=hour, completed=counts.get(hour, 0))
        for hour in (start + timedelta(hours=i) for i in range(hours))
    ]


def execution_seconds(timings: Sequence[CompletedTiming]) -> float:
    """Total GPU execution time over a window of completions.

    Args:
        timings: The window's timings, any order.

    Returns:
        The sum of `inference_seconds`, 0.0 for an empty window.
    """
    return sum(timing.inference_seconds for timing in timings)


def window_span(
    timings: Sequence[CompletedTiming],
) -> tuple[datetime | None, datetime | None]:
    """Bound a window of completions in time.

    Computed from the same timings the cost prices, not from a second query:
    a span fetched separately could cover a different set of jobs than the
    cost it is meant to explain.

    Args:
        timings: The window's timings, any order.

    Returns:
        Oldest and newest `completed_at`, both None for an empty window.
    """
    if not timings:
        return None, None
    completions = [timing.completed_at for timing in timings]
    return min(completions), max(completions)


def estimated_cost_usd(execution_seconds: float, rate_usd_hr: float) -> float:
    """Price execution time at an hourly GPU rate.

    Args:
        execution_seconds: Total GPU execution time.
        rate_usd_hr: The hourly rate in USD.

    Returns:
        The estimated cost in USD.
    """
    return execution_seconds * rate_usd_hr / SECONDS_PER_HOUR
