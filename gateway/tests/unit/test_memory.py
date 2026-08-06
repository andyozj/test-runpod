"""Repository retention: terminal jobs are evicted, live ones never are."""

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
