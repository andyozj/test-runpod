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


def make_job(status: JobStatus = JobStatus.QUEUED, api_key_id: str = "demo") -> Job:
    params = GenerationParams(prompt="a fox")
    return Job(
        id=uuid.uuid4(),
        status=status,
        params=params,
        context=RequestContext(api_key_id=api_key_id, correlation_id="c-1"),
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


async def test_releasing_an_idempotency_key_frees_it_for_a_new_job(
    clock: FrozenClock,
) -> None:
    repository = InMemoryJobRepository(clock=clock)
    first = make_job()
    first = first.advanced(
        context=RequestContext(
            api_key_id="demo", correlation_id="c-1", idempotency_key="k1"
        )
    )
    await repository.create(first)

    await repository.release_idempotency_key(first.id)
    second = make_job()
    second = second.advanced(context=first.context)
    stored = await repository.create(second)

    assert stored.id == second.id


async def test_releasing_a_key_leaves_other_keys_alone(clock: FrozenClock) -> None:
    repository = InMemoryJobRepository(clock=clock)
    kept = make_job()
    kept = kept.advanced(
        context=RequestContext(
            api_key_id="demo", correlation_id="c-1", idempotency_key="k1"
        )
    )
    await repository.create(kept)

    await repository.release_idempotency_key(uuid.uuid4())
    replay = make_job()
    replay = replay.advanced(context=kept.context)
    stored = await repository.create(replay)

    assert stored.id == kept.id


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


async def test_claims_are_oldest_first(clock: FrozenClock) -> None:
    """Newest-first starves the oldest job forever once the backlog exceeds the limit."""
    repository = InMemoryJobRepository(clock=clock)
    order = []
    for index in range(3):
        job = await repository.create(make_job())
        order.append((await repository.attach_runpod_id(job.id, f"up-{index}")).id)
        clock.advance(10)

    claimed = await claim(repository)

    assert [job.id for job in claimed] == order


async def test_the_limit_caps_a_claim_at_the_oldest_n(clock: FrozenClock) -> None:
    repository = InMemoryJobRepository(clock=clock)
    order = []
    for index in range(5):
        job = await repository.create(make_job())
        order.append((await repository.attach_runpod_id(job.id, f"up-{index}")).id)
        clock.advance(10)

    claimed = await claim(repository, limit=2)

    assert [job.id for job in claimed] == order[:2]


async def test_the_unclaimed_remainder_is_taken_by_the_next_call(
    clock: FrozenClock,
) -> None:
    """The leases from the first claim must not hide the backlog from the second."""
    repository = InMemoryJobRepository(clock=clock)
    order = []
    for index in range(4):
        job = await repository.create(make_job())
        order.append((await repository.attach_runpod_id(job.id, f"up-{index}")).id)
        clock.advance(10)

    first = await claim(repository, limit=2)
    second = await claim(repository, limit=2)

    assert [job.id for job in first] == order[:2]
    assert [job.id for job in second] == order[2:]


async def test_a_limit_of_zero_claims_nothing(clock: FrozenClock) -> None:
    repository = InMemoryJobRepository(clock=clock)
    await submitted_job(repository)

    assert await claim(repository, limit=0) == []


# --- Idempotency-key eviction ---


def keyed_job(key: str, prompt: str = "a fox") -> Job:
    params = GenerationParams(prompt=prompt)
    return Job(
        id=uuid.uuid4(),
        status=JobStatus.QUEUED,
        params=params,
        context=RequestContext(
            api_key_id="demo", correlation_id="c-1", idempotency_key=key
        ),
        created_at=FROZEN,
        updated_at=FROZEN,
        request_hash=params.fingerprint(),
    )


async def test_an_evicted_jobs_idempotency_key_is_forgotten(
    clock: FrozenClock,
) -> None:
    """A key still pointing at a dropped job resolves to nothing that exists."""
    repository = InMemoryJobRepository(clock=clock, retention_s=3600)
    old = await repository.create(keyed_job("k-1"))
    await repository.mark_failed(
        old.id, ErrorCode.JOB_CANCELLED, "cancelled", JobStatus.CANCELLED
    )

    clock.advance(3601)
    replacement = await repository.create(keyed_job("k-1"))

    assert replacement.id != old.id
    assert await repository.get(replacement.id) is not None


async def test_a_live_jobs_idempotency_key_survives_an_eviction_sweep(
    clock: FrozenClock,
) -> None:
    """The sweep must drop only the keys of the jobs it actually removed."""
    repository = InMemoryJobRepository(clock=clock, retention_s=3600)
    doomed = await repository.create(keyed_job("k-doomed"))
    await repository.mark_failed(
        doomed.id, ErrorCode.JOB_CANCELLED, "cancelled", JobStatus.CANCELLED
    )
    live = await repository.create(keyed_job("k-live"))

    clock.advance(3601)
    replayed = await repository.create(keyed_job("k-live"))

    assert replayed.id == live.id
    assert await repository.get(doomed.id) is None


async def test_an_evicted_key_no_longer_reports_a_body_conflict(
    clock: FrozenClock,
) -> None:
    """Conflicting with a job nobody can fetch is a 409 the caller cannot act on."""
    repository = InMemoryJobRepository(clock=clock, retention_s=3600)
    old = await repository.create(keyed_job("k-1", prompt="a fox"))
    await repository.mark_failed(
        old.id, ErrorCode.JOB_CANCELLED, "cancelled", JobStatus.CANCELLED
    )

    clock.advance(3601)
    fresh = await repository.create(
        keyed_job("k-1", prompt="a completely different cat")
    )

    assert fresh.id != old.id


async def test_count_active_counts_only_the_key_and_non_terminal_jobs(
    clock: FrozenClock,
) -> None:
    repository = InMemoryJobRepository(clock=clock)
    await repository.create(make_job())
    terminal = await repository.create(make_job())
    await repository.mark_failed(
        terminal.id, ErrorCode.JOB_CANCELLED, "cancelled", JobStatus.CANCELLED
    )
    await repository.create(make_job(api_key_id="other"))

    assert await repository.count_active("demo") == 1
    assert await repository.count_active("other") == 1
    assert await repository.count_active("nobody") == 0
