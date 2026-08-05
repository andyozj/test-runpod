"""JobService: submit, reconcile, timeouts, orphans, queue pressure."""

from __future__ import annotations

import asyncio

import pytest

from gateway.core.models import (
    ErrorCode,
    GenerationParams,
    JobStatus,
    RequestContext,
)
from gateway.core.protocols import EndpointHealth, IdempotencyConflictError
from gateway.core.service import JobService, QueueSaturatedError
from tests.conftest import (
    FakeGuardrail,
    FakeRunPodClient,
    FrozenClock,
    Verdict,
    completed,
    failed,
    in_progress,
)

PARAMS = GenerationParams(prompt="a red fox")


def ctx(key: str | None = None, api_key_id: str = "demo") -> RequestContext:
    return RequestContext(
        api_key_id=api_key_id, correlation_id="corr-1", idempotency_key=key
    )


async def test_submit_records_and_dispatches(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    job = await service.submit(PARAMS, ctx())

    assert job.status is JobStatus.QUEUED
    assert job.runpod_job_id == "runpod-1"
    assert runpod.submissions[0]["prompt"] == "a red fox"
    assert runpod.submissions[0]["correlation_id"] == "corr-1"


async def test_blocked_prompt_is_recorded_and_never_submitted(
    service: JobService, runpod: FakeRunPodClient, guardrail: FakeGuardrail
) -> None:
    guardrail.verdict = Verdict(blocked=True, reason="nope")

    job = await service.submit(PARAMS, ctx())

    assert job.status is JobStatus.BLOCKED
    assert job.error_code is ErrorCode.PROMPT_BLOCKED
    assert runpod.submissions == []


async def test_idempotent_replay_returns_the_original_job(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    first = await service.submit(PARAMS, ctx(key="k1"))
    second = await service.submit(PARAMS, ctx(key="k1"))

    assert first.id == second.id
    assert len(runpod.submissions) == 1


async def test_concurrent_identical_submissions_generate_once(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    jobs = await asyncio.gather(
        *(service.submit(PARAMS, ctx(key="race")) for _ in range(8))
    )

    assert len({job.id for job in jobs}) == 1
    assert len(runpod.submissions) == 1


async def test_same_key_different_body_is_a_conflict(service: JobService) -> None:
    await service.submit(PARAMS, ctx(key="k2"))

    with pytest.raises(IdempotencyConflictError):
        await service.submit(GenerationParams(prompt="a blue fox"), ctx(key="k2"))


async def test_keys_are_scoped_per_caller(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    mine = await service.submit(PARAMS, ctx(key="shared", api_key_id="demo"))
    theirs = await service.submit(PARAMS, ctx(key="shared", api_key_id="other"))

    assert mine.id != theirs.id
    assert len(runpod.submissions) == 2


async def test_reconcile_completes_a_finished_job(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    job = await service.submit(PARAMS, ctx())
    runpod.next_status = completed(seed=7)

    assert await service.reconcile() == 1
    stored = await service.get(job.id)
    assert stored is not None
    assert stored.status is JobStatus.COMPLETED
    assert stored.result is not None
    assert stored.result.seed == 7


async def test_reconcile_stores_progress_without_completing(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    job = await service.submit(PARAMS, ctx())
    runpod.next_status = in_progress(12, 28)

    await service.reconcile()

    stored = await service.get(job.id)
    assert stored is not None
    assert stored.status is JobStatus.IN_PROGRESS
    assert stored.progress is not None
    assert stored.progress.percent == 43


async def test_unreachable_upstream_leaves_the_job_untouched(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    job = await service.submit(PARAMS, ctx())
    runpod.status_raises = ConnectionError("network")

    assert await service.reconcile() == 0
    stored = await service.get(job.id)
    assert stored is not None
    assert stored.status is JobStatus.QUEUED


async def test_terminal_jobs_are_never_moved_backwards(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    job = await service.submit(PARAMS, ctx())
    runpod.next_status = completed()
    await service.reconcile()

    runpod.next_status = in_progress(1, 28)
    await service.reconcile()

    stored = await service.get(job.id)
    assert stored is not None
    assert stored.status is JobStatus.COMPLETED


async def test_failed_upstream_becomes_a_terminal_failure(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    job = await service.submit(PARAMS, ctx())
    runpod.next_status = failed(ErrorCode.OOM)

    await service.reconcile()

    stored = await service.get(job.id)
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    assert stored.error_code is ErrorCode.OOM


async def test_job_past_the_deadline_times_out(
    service: JobService, runpod: FakeRunPodClient, clock: FrozenClock
) -> None:
    job = await service.submit(PARAMS, ctx())
    clock.advance(601)

    await service.reconcile()

    stored = await service.get(job.id)
    assert stored is not None
    assert stored.status is JobStatus.TIMED_OUT
    assert stored.error_code is ErrorCode.JOB_TIMEOUT


async def test_job_inside_the_deadline_does_not_time_out(
    service: JobService, runpod: FakeRunPodClient, clock: FrozenClock
) -> None:
    await service.submit(PARAMS, ctx())
    clock.advance(599)

    await service.reconcile()

    assert all(
        job.status is not JobStatus.TIMED_OUT
        for job in await service.repository.claim_unresolved(10)
    )


async def test_orphaned_job_is_resubmitted_rather_than_polled(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    runpod.submit_raises = ConnectionError("died after insert")
    with pytest.raises(ConnectionError):
        await service.submit(PARAMS, ctx())

    runpod.submit_raises = None
    assert await service.reconcile() == 1
    assert len(runpod.submissions) == 1


async def test_one_bad_job_does_not_abort_the_tick(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    await service.submit(PARAMS, ctx())
    await service.submit(GenerationParams(prompt="second"), ctx())
    runpod.status_raises = ValueError("unknown RunPod status")

    assert await service.reconcile() == 0


async def test_queue_pressure_sheds_when_the_wait_is_too_long(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    runpod.health_value = EndpointHealth(
        in_queue=100, in_progress=1, workers_running=1, workers_idle=0
    )
    await service.reconcile()

    with pytest.raises(QueueSaturatedError) as exc:
        await service.submit(PARAMS, ctx())

    assert exc.value.retry_after_s > 0


async def test_queue_pressure_fails_open_when_health_is_unavailable(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    runpod.health_raises = ConnectionError("no reading")
    await service.reconcile()

    job = await service.submit(PARAMS, ctx())

    assert job.status is JobStatus.QUEUED


async def test_light_queue_does_not_shed(
    service: JobService, runpod: FakeRunPodClient
) -> None:
    runpod.health_value = EndpointHealth(
        in_queue=2, in_progress=0, workers_running=0, workers_idle=3
    )
    await service.reconcile()

    assert (await service.submit(PARAMS, ctx())).status is JobStatus.QUEUED


async def test_unknown_job_returns_none(service: JobService) -> None:
    from uuid import uuid4

    assert await service.get(uuid4()) is None
