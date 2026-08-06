"""Repository retention and claim leases."""

from __future__ import annotations

import uuid

from gateway.adapters.memory import InMemoryJobRepository
from gateway.core.models import (
    ErrorCode,
    GenerationParams,
    Job,
    JobStatus,
    RequestContext,
)
from tests.conftest import FROZEN, FrozenClock


def make_job(status: JobStatus = JobStatus.QUEUED) -> Job:
    params = GenerationParams(prompt="a fox")
    return Job(
        id=uuid.uuid4(),
        status=status,
        params=params,
        context=RequestContext(api_key_id="demo", correlation_id="c-1"),
        created_at=FROZEN,
        updated_at=FROZEN,
        request_hash=params.fingerprint(),
    )


async def test_terminal_jobs_are_evicted_after_retention(clock: FrozenClock) -> None:
    repository = InMemoryJobRepository(clock=clock, retention_s=3600)
    old = await repository.create(make_job())
    await repository.mark_failed(
        old.id, ErrorCode.JOB_CANCELLED, "cancelled", JobStatus.CANCELLED
    )

    clock.advance(3601)
    await repository.create(make_job())

    assert await repository.get(old.id) is None


async def test_live_jobs_survive_retention(clock: FrozenClock) -> None:
    repository = InMemoryJobRepository(clock=clock, retention_s=3600)
    live = await repository.create(make_job())

    clock.advance(7200)
    await repository.create(make_job())

    assert await repository.get(live.id) is not None


LEASE_S = 60.0
GRACE_S = 30.0


async def claim(repository: InMemoryJobRepository, limit: int = 10) -> list[Job]:
    return await repository.claim_unresolved(
        limit, lease_s=LEASE_S, submit_grace_s=GRACE_S
    )


async def submitted_job(repository: InMemoryJobRepository) -> Job:
    job = await repository.create(make_job())
    return await repository.attach_runpod_id(job.id, "up-1")


async def test_a_claimed_job_is_not_claimed_again_while_the_lease_holds(
    clock: FrozenClock,
) -> None:
    repository = InMemoryJobRepository(clock=clock)
    job = await submitted_job(repository)

    assert [claimed.id for claimed in await claim(repository)] == [job.id]
    assert await claim(repository) == []


async def test_an_expired_lease_makes_the_job_claimable_again(
    clock: FrozenClock,
) -> None:
    repository = InMemoryJobRepository(clock=clock)
    await submitted_job(repository)
    await claim(repository)

    clock.advance(61)

    assert len(await claim(repository)) == 1


async def test_releasing_a_claim_makes_the_job_immediately_claimable(
    clock: FrozenClock,
) -> None:
    repository = InMemoryJobRepository(clock=clock)
    job = await submitted_job(repository)
    await claim(repository)

    await repository.release_claim(job.id)

    assert len(await claim(repository)) == 1


async def test_releasing_an_unknown_claim_is_a_no_op(clock: FrozenClock) -> None:
    repository = InMemoryJobRepository(clock=clock)

    await repository.release_claim(uuid.uuid4())


async def test_a_job_without_an_upstream_id_is_held_for_the_grace_period(
    clock: FrozenClock,
) -> None:
    """Its submit may still be in flight; claiming it would double-submit."""
    repository = InMemoryJobRepository(clock=clock)
    await repository.create(make_job())

    assert await claim(repository) == []

    clock.advance(31)
    assert len(await claim(repository)) == 1


async def test_terminal_jobs_are_never_claimed(clock: FrozenClock) -> None:
    repository = InMemoryJobRepository(clock=clock)
    job = await submitted_job(repository)
    await repository.mark_failed(
        job.id, ErrorCode.JOB_CANCELLED, "cancelled", JobStatus.CANCELLED
    )

    assert await claim(repository) == []


async def test_the_upstream_id_is_recorded_even_on_a_cancelled_job(
    clock: FrozenClock,
) -> None:
    """Without the id there is no way to stop the GPU work already started."""
    repository = InMemoryJobRepository(clock=clock)
    job = await repository.create(make_job())
    await repository.mark_failed(
        job.id, ErrorCode.JOB_CANCELLED, "cancelled", JobStatus.CANCELLED
    )

    attached = await repository.attach_runpod_id(job.id, "up-9")

    assert attached.runpod_job_id == "up-9"
    assert attached.status is JobStatus.CANCELLED
