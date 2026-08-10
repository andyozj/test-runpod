"""The JobRepository contract, run identically against every adapter.

Moved out of tests/unit/test_memory.py so the Postgres adapter proves the
same invariants the memory adapter does; memory-only behaviour (retention
eviction) stays there.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from gateway.core.models import (
    ErrorCode,
    GenerationParams,
    Job,
    JobResult,
    JobStatus,
    Progress,
    RequestContext,
)
from gateway.core.protocols import IdempotencyConflictError, JobRepository
from tests.conftest import FROZEN, FrozenClock, keyed_job, make_job

LEASE_S = 60.0
GRACE_S = 30.0


def result(seed: int = 42) -> JobResult:
    return JobResult(
        image_base64="aGVsbG8=",
        format="png",
        seed=seed,
        width=1024,
        height=1024,
        model_version="black-forest-labs/FLUX.1-dev@0ef5fff",
        inference_seconds=21.4,
    )


async def claim(repository: JobRepository, limit: int = 10) -> list[Job]:
    return await repository.claim_unresolved(
        limit, lease_s=LEASE_S, submit_grace_s=GRACE_S
    )


async def submitted_job(repository: JobRepository) -> Job:
    job = await repository.create(make_job())
    return await repository.attach_runpod_id(job.id, "up-1")


# --- Insert and read-back ---


async def test_a_created_job_reads_back_identically(
    repository: JobRepository,
) -> None:
    created = await repository.create(make_job())

    fetched = await repository.get(created.id)

    assert fetched == created


async def test_an_unknown_id_reads_as_none(repository: JobRepository) -> None:
    assert await repository.get(uuid.uuid4()) is None


async def test_a_result_round_trips_through_completion(
    repository: JobRepository,
) -> None:
    job = await submitted_job(repository)

    await repository.mark_completed(job.id, result(seed=7))
    stored = await repository.get(job.id)

    assert stored is not None
    assert stored.status is JobStatus.COMPLETED
    assert stored.result == result(seed=7)
    assert stored.completed_at == FROZEN


async def test_progress_round_trips(repository: JobRepository) -> None:
    job = await submitted_job(repository)

    await repository.mark_in_progress(job.id, Progress(step=7, total=28, percent=25))
    stored = await repository.get(job.id)

    assert stored is not None
    assert stored.status is JobStatus.IN_PROGRESS
    assert stored.progress == Progress(step=7, total=28, percent=25)


PREVIEWED = Progress(
    step=14, total=28, percent=50, preview_b64="ZnJhbWU=", preview_format="jpeg"
)


async def test_a_preview_frame_round_trips_while_running(
    repository: JobRepository,
) -> None:
    job = await submitted_job(repository)

    await repository.mark_in_progress(job.id, PREVIEWED)
    stored = await repository.get(job.id)

    assert stored is not None
    assert stored.progress == PREVIEWED


async def test_completion_strips_the_preview_frame(
    repository: JobRepository,
) -> None:
    """A preview is transient telemetry; the stored image supersedes it."""
    job = await submitted_job(repository)
    await repository.mark_in_progress(job.id, PREVIEWED)

    await repository.mark_completed(job.id, result())
    stored = await repository.get(job.id)

    assert stored is not None
    assert stored.progress == PREVIEWED.without_preview()


async def test_failure_strips_the_preview_frame(repository: JobRepository) -> None:
    job = await submitted_job(repository)
    await repository.mark_in_progress(job.id, PREVIEWED)

    await repository.mark_failed(
        job.id, ErrorCode.INFERENCE_FAILED, "boom", JobStatus.FAILED
    )
    stored = await repository.get(job.id)

    assert stored is not None
    assert stored.progress == PREVIEWED.without_preview()


async def test_terminal_jobs_do_not_transition_again(
    repository: JobRepository,
) -> None:
    job = await submitted_job(repository)
    await repository.mark_completed(job.id, result())

    after = await repository.mark_failed(
        job.id, ErrorCode.INFERENCE_FAILED, "boom", JobStatus.FAILED
    )

    assert after.status is JobStatus.COMPLETED
    assert after.result == result()


# --- Idempotency ---


async def test_a_replayed_key_returns_the_original_job(
    repository: JobRepository,
) -> None:
    first = await repository.create(keyed_job("k-1"))

    replay = await repository.create(keyed_job("k-1"))

    assert replay.id == first.id


async def test_a_replayed_key_with_a_different_body_conflicts(
    repository: JobRepository,
) -> None:
    await repository.create(keyed_job("k-1", prompt="a fox"))

    with pytest.raises(IdempotencyConflictError):
        await repository.create(keyed_job("k-1", prompt="a cat"))


async def test_the_same_key_from_different_callers_does_not_collide(
    repository: JobRepository,
) -> None:
    """Key scoping is per caller; a global scope would leak one caller's image."""
    mine = keyed_job("shared")
    theirs = keyed_job("shared")
    theirs = theirs.advanced(
        context=RequestContext(
            api_key_id="other", correlation_id="c-2", idempotency_key="shared"
        )
    )

    first = await repository.create(mine)
    second = await repository.create(theirs)

    assert first.id != second.id


async def test_jobs_without_keys_never_replay(repository: JobRepository) -> None:
    first = await repository.create(make_job())
    second = await repository.create(make_job())

    assert first.id != second.id


async def test_releasing_an_idempotency_key_frees_it_for_a_new_job(
    repository: JobRepository,
) -> None:
    first = await repository.create(keyed_job("k-1"))

    await repository.release_idempotency_key(first.id)
    second = await repository.create(keyed_job("k-1"))

    assert second.id != first.id


async def test_releasing_a_key_leaves_other_keys_alone(
    repository: JobRepository,
) -> None:
    kept = await repository.create(keyed_job("k-1"))

    await repository.release_idempotency_key(uuid.uuid4())
    replay = await repository.create(keyed_job("k-1"))

    assert replay.id == kept.id


# --- Claim leases ---


async def test_a_claimed_job_is_not_claimed_again_while_the_lease_holds(
    repository: JobRepository,
) -> None:
    job = await submitted_job(repository)

    assert [claimed.id for claimed in await claim(repository)] == [job.id]
    assert await claim(repository) == []


async def test_an_expired_lease_makes_the_job_claimable_again(
    repository: JobRepository, clock: FrozenClock
) -> None:
    await submitted_job(repository)
    await claim(repository)

    clock.advance(61)

    assert len(await claim(repository)) == 1


async def test_releasing_a_claim_makes_the_job_immediately_claimable(
    repository: JobRepository,
) -> None:
    job = await submitted_job(repository)
    await claim(repository)

    await repository.release_claim(job.id)

    assert len(await claim(repository)) == 1


async def test_releasing_an_unknown_claim_is_a_no_op(
    repository: JobRepository,
) -> None:
    await repository.release_claim(uuid.uuid4())


async def test_a_job_without_an_upstream_id_is_held_for_the_grace_period(
    repository: JobRepository, clock: FrozenClock
) -> None:
    """Its submit may still be in flight; claiming it would double-submit."""
    await repository.create(make_job())

    assert await claim(repository) == []

    clock.advance(31)
    assert len(await claim(repository)) == 1


async def test_terminal_jobs_are_never_claimed(repository: JobRepository) -> None:
    job = await submitted_job(repository)
    await repository.mark_failed(
        job.id, ErrorCode.JOB_CANCELLED, "cancelled", JobStatus.CANCELLED
    )

    assert await claim(repository) == []


async def test_the_upstream_id_is_recorded_even_on_a_cancelled_job(
    repository: JobRepository,
) -> None:
    """Without the id there is no way to stop the GPU work already started."""
    job = await repository.create(make_job())
    await repository.mark_failed(
        job.id, ErrorCode.JOB_CANCELLED, "cancelled", JobStatus.CANCELLED
    )

    attached = await repository.attach_runpod_id(job.id, "up-9")

    assert attached.runpod_job_id == "up-9"
    assert attached.status is JobStatus.CANCELLED


async def test_claims_are_oldest_first(
    repository: JobRepository, clock: FrozenClock
) -> None:
    """Newest-first starves the oldest job forever once the backlog exceeds the limit."""
    order = []
    for index in range(3):
        job = await repository.create(make_job())
        order.append((await repository.attach_runpod_id(job.id, f"up-{index}")).id)
        clock.advance(10)

    claimed = await claim(repository)

    assert [job.id for job in claimed] == order


async def test_the_limit_caps_a_claim_at_the_oldest_n(
    repository: JobRepository, clock: FrozenClock
) -> None:
    order = []
    for index in range(5):
        job = await repository.create(make_job())
        order.append((await repository.attach_runpod_id(job.id, f"up-{index}")).id)
        clock.advance(10)

    claimed = await claim(repository, limit=2)

    assert [job.id for job in claimed] == order[:2]


async def test_the_unclaimed_remainder_is_taken_by_the_next_call(
    repository: JobRepository, clock: FrozenClock
) -> None:
    """The leases from the first claim must not hide the backlog from the second."""
    order = []
    for index in range(4):
        job = await repository.create(make_job())
        order.append((await repository.attach_runpod_id(job.id, f"up-{index}")).id)
        clock.advance(10)

    first = await claim(repository, limit=2)
    second = await claim(repository, limit=2)

    assert [job.id for job in first] == order[:2]
    assert [job.id for job in second] == order[2:]


async def test_a_limit_of_zero_claims_nothing(repository: JobRepository) -> None:
    await submitted_job(repository)

    assert await claim(repository, limit=0) == []


# --- Active-job counting ---


async def test_count_active_counts_only_the_key_and_non_terminal_jobs(
    repository: JobRepository,
) -> None:
    await repository.create(make_job())
    terminal = await repository.create(make_job())
    await repository.mark_failed(
        terminal.id, ErrorCode.JOB_CANCELLED, "cancelled", JobStatus.CANCELLED
    )
    await repository.create(make_job(api_key_id="other"))

    assert await repository.count_active("demo") == 1
    assert await repository.count_active("other") == 1
    assert await repository.count_active("nobody") == 0


# --- Listing ---


def _job_at(clock: FrozenClock, api_key_id: str = "demo") -> Job:
    """A job stamped with the clock's current time, so listings order fully."""
    return make_job(api_key_id=api_key_id).advanced(
        created_at=clock.now(), updated_at=clock.now()
    )


async def test_list_recent_is_newest_first_and_caller_scoped(
    repository: JobRepository, clock: FrozenClock
) -> None:
    order = []
    for _ in range(3):
        order.append((await repository.create(_job_at(clock))).id)
        clock.advance(10)
    await repository.create(_job_at(clock, api_key_id="other"))

    listed = await repository.list_recent("demo", limit=10)

    assert [job.id for job in listed] == list(reversed(order))


async def test_list_recent_caps_at_the_limit(
    repository: JobRepository, clock: FrozenClock
) -> None:
    newest = None
    for _ in range(3):
        newest = await repository.create(_job_at(clock))
        clock.advance(10)

    listed = await repository.list_recent("demo", limit=1)

    assert newest is not None
    assert [job.id for job in listed] == [newest.id]


async def test_list_recent_for_an_unknown_caller_is_empty(
    repository: JobRepository,
) -> None:
    await repository.create(make_job())

    assert await repository.list_recent("nobody", limit=10) == []


# --- Metrics queries ---


async def completed_at(
    repository: JobRepository,
    clock: FrozenClock,
    inference_s: float,
    api_key_id: str = "demo",
) -> Job:
    """Create, submit and complete one job stamped with the clock's time."""
    job = await repository.create(_job_at(clock, api_key_id=api_key_id))
    await repository.attach_runpod_id(job.id, f"up-{job.id}")
    return await repository.mark_completed(
        job.id,
        JobResult(
            image_base64="aGVsbG8=",
            format="png",
            seed=1,
            width=1024,
            height=1024,
            model_version="black-forest-labs/FLUX.1-dev@0ef5fff",
            inference_seconds=inference_s,
        ),
    )


async def test_count_by_status_zero_fills_and_scopes_by_caller(
    repository: JobRepository,
) -> None:
    await repository.create(make_job())
    await repository.create(make_job())
    failed = await repository.create(make_job())
    await repository.mark_failed(
        failed.id, ErrorCode.INFERENCE_FAILED, "boom", JobStatus.FAILED
    )
    await repository.create(make_job(api_key_id="other"))

    counts = await repository.count_by_status("demo")

    assert counts[JobStatus.QUEUED] == 2
    assert counts[JobStatus.FAILED] == 1
    assert set(counts) == set(JobStatus)
    assert sum(counts.values()) == 3


async def test_count_created_since_is_inclusive_and_caller_scoped(
    repository: JobRepository, clock: FrozenClock
) -> None:
    await repository.create(_job_at(clock))
    clock.advance(3600)
    cutoff = clock.now()
    await repository.create(_job_at(clock))
    await repository.create(_job_at(clock, api_key_id="other"))
    clock.advance(60)
    await repository.create(_job_at(clock))

    assert await repository.count_created_since("demo", cutoff) == 2
    assert await repository.count_created_since("other", cutoff) == 1


async def test_recent_completed_timings_are_newest_first_with_wall_and_inference(
    repository: JobRepository, clock: FrozenClock
) -> None:
    first = await completed_at(repository, clock, inference_s=10.0)
    clock.advance(600)
    await completed_at(repository, clock, inference_s=20.0)
    await repository.create(_job_at(clock))  # never completed, never listed
    assert first.completed_at is not None

    timings = await repository.recent_completed_timings("demo", limit=10)

    assert [timing.inference_seconds for timing in timings] == [20.0, 10.0]
    assert timings[1].created_at == first.created_at
    assert timings[1].completed_at == first.completed_at
    assert timings[1].wall_seconds == 0.0


async def test_recent_completed_timings_bound_the_window_in_time(
    repository: JobRepository, clock: FrozenClock
) -> None:
    """`window_started_at`/`window_ended_at` are min/max of exactly these."""
    oldest = clock.now()
    await completed_at(repository, clock, inference_s=10.0)
    clock.advance(600)
    await completed_at(repository, clock, inference_s=20.0)
    clock.advance(600)
    newest = clock.now()
    await completed_at(repository, clock, inference_s=30.0)

    timings = await repository.recent_completed_timings("demo", limit=10)
    completions = [timing.completed_at for timing in timings]

    assert min(completions) == oldest
    assert max(completions) == newest
    assert all(moment.tzinfo is not None for moment in completions)
    assert sum(timing.inference_seconds for timing in timings) == 60.0


async def test_recent_completed_timings_cap_at_the_limit(
    repository: JobRepository, clock: FrozenClock
) -> None:
    for inference_s in (10.0, 20.0, 30.0):
        await completed_at(repository, clock, inference_s=inference_s)
        clock.advance(60)

    timings = await repository.recent_completed_timings("demo", limit=2)

    assert [timing.inference_seconds for timing in timings] == [30.0, 20.0]


async def test_recent_completed_timings_are_caller_scoped(
    repository: JobRepository, clock: FrozenClock
) -> None:
    await completed_at(repository, clock, inference_s=10.0)
    await completed_at(repository, clock, inference_s=99.0, api_key_id="other")

    timings = await repository.recent_completed_timings("demo", limit=10)

    assert [timing.inference_seconds for timing in timings] == [10.0]


async def test_completions_are_counted_per_utc_hour(
    repository: JobRepository, clock: FrozenClock
) -> None:
    """Completions stay within the memory adapter's retention mid-arrange."""
    since = clock.now()
    hour = since.replace(minute=0, second=0, microsecond=0)
    clock.advance(3000)
    await completed_at(repository, clock, inference_s=10.0)
    clock.advance(120)
    await completed_at(repository, clock, inference_s=10.0)
    clock.advance(600)
    await completed_at(repository, clock, inference_s=10.0)
    await completed_at(repository, clock, inference_s=10.0, api_key_id="other")

    counts = await repository.count_completed_by_hour("demo", since)

    assert counts == {hour: 2, hour + timedelta(hours=1): 1}


async def test_completions_before_the_cutoff_are_not_counted(
    repository: JobRepository, clock: FrozenClock
) -> None:
    await completed_at(repository, clock, inference_s=10.0)
    clock.advance(7200)

    assert await repository.count_completed_by_hour("demo", clock.now()) == {}


async def test_a_generation_params_round_trips_every_field(
    repository: JobRepository,
) -> None:
    params = GenerationParams(
        prompt="a fox",
        width=768,
        height=512,
        num_inference_steps=12,
        guidance_scale=2.5,
        seed=99,
        output_format="jpeg",
    )
    job = make_job().advanced(params=params, request_hash=params.fingerprint())

    stored = await repository.create(job)
    fetched = await repository.get(stored.id)

    assert fetched is not None
    assert fetched.params == params
