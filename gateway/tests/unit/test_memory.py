"""Memory-only behaviour: retention eviction of terminal jobs and their keys.

The adapter-agnostic invariants (idempotent insert, claim leases, counting,
listing) live in tests/contract/test_job_repository.py and run against every
adapter. Retention is deliberately not contractual: Postgres keeps terminal
jobs — that is the point of the gallery — while the memory adapter must evict
them or grow until OOM.
"""

from __future__ import annotations

import uuid

from gateway.adapters.memory import InMemoryJobRepository
from gateway.core.models import ErrorCode, JobStatus
from tests.conftest import FrozenClock, keyed_job, make_job


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


async def test_an_evicted_job_no_longer_appears_in_the_listing(
    clock: FrozenClock,
) -> None:
    repository = InMemoryJobRepository(clock=clock, retention_s=3600)
    old = await repository.create(make_job())
    await repository.mark_failed(
        old.id, ErrorCode.JOB_CANCELLED, "cancelled", JobStatus.CANCELLED
    )

    clock.advance(3601)
    await repository.create(make_job())

    listed = await repository.list_recent("demo", limit=10)
    assert old.id not in [job.id for job in listed]
    assert len(listed) == 1


async def test_releasing_a_claim_on_an_unknown_job_is_a_no_op(
    clock: FrozenClock,
) -> None:
    repository = InMemoryJobRepository(clock=clock)

    await repository.release_claim(uuid.uuid4())
