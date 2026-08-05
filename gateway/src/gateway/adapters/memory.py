"""In-memory job repository.

Postgres is the specified production store (docs/specs/02-gateway-core.md);
this implements the same protocol so the service, the API and the reconciler
are fully exercised without a database. Swapping it is one binding in the
composition root.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from gateway.core.models import (
    ErrorCode,
    Job,
    JobResult,
    JobStatus,
    Progress,
)
from gateway.core.protocols import Clock, IdempotencyConflictError


@dataclass
class InMemoryJobRepository:
    """Job storage backed by a dict, guarded by a lock.

    The lock is what makes the idempotency check atomic. A read-then-write
    without it has exactly the race idempotency exists to prevent: two
    concurrent identical requests both see no existing row and both submit,
    double-billing the GPU.

    Attributes:
        clock: Injected wall time, so `updated_at` is assertable.
    """

    clock: Clock
    _jobs: dict[UUID, Job] = field(default_factory=dict)
    _by_key: dict[tuple[str, str], UUID] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def create(self, job: Job) -> Job:
        """Insert a job, replaying on a repeated idempotency key.

        The key is scoped by `api_key_id`. Scoped to the key alone, two callers
        choosing the same value would collide and one would receive the other's
        image — a data leak, not an inconvenience.

        Args:
            job: The job to store.

        Returns:
            The stored job, or the existing one on a matching replay.

        Raises:
            IdempotencyConflictError: The key was reused with a different body.
        """
        key = job.context.idempotency_key
        async with self._lock:
            if key is not None:
                scoped = (job.context.api_key_id, key)
                existing_id = self._by_key.get(scoped)
                if existing_id is not None:
                    existing = self._jobs[existing_id]
                    if existing.request_hash != job.request_hash:
                        raise IdempotencyConflictError
                    return existing
                self._by_key[scoped] = job.id
            self._jobs[job.id] = job
            return job

    async def get(self, job_id: UUID) -> Job | None:
        """Fetch one job.

        Args:
            job_id: The id to look up.

        Returns:
            The job, or None if unknown.
        """
        return self._jobs.get(job_id)

    async def attach_runpod_id(self, job_id: UUID, runpod_job_id: str) -> Job:
        """Record the upstream job id.

        Args:
            job_id: The job to update.
            runpod_job_id: The upstream identifier.

        Returns:
            The updated job.
        """
        return await self._update(job_id, runpod_job_id=runpod_job_id)

    async def mark_in_progress(self, job_id: UUID, progress: Progress | None) -> Job:
        """Advance a job to running, storing progress when reported.

        Progress never alters a terminal job, and never changes `status` on its
        own beyond the queued-to-running transition.

        Args:
            job_id: The job to update.
            progress: Latest progress, if reported.

        Returns:
            The updated job.
        """
        return await self._update(
            job_id, status=JobStatus.IN_PROGRESS, progress=progress
        )

    async def mark_completed(self, job_id: UUID, result: JobResult) -> Job:
        """Record a successful result.

        Args:
            job_id: The job to update.
            result: What the worker produced.

        Returns:
            The updated job.
        """
        return await self._update(
            job_id,
            status=JobStatus.COMPLETED,
            result=result,
            completed_at=self.clock.now(),
        )

    async def mark_failed(
        self, job_id: UUID, code: ErrorCode, message: str, status: JobStatus
    ) -> Job:
        """Record a terminal failure.

        Args:
            job_id: The job to update.
            code: The stable error code.
            message: Caller-safe description.
            status: The terminal status to write.

        Returns:
            The updated job.
        """
        return await self._update(
            job_id,
            status=status,
            error_code=code,
            error_message=message,
            completed_at=self.clock.now(),
        )

    async def claim_unresolved(self, limit: int) -> list[Job]:
        """Claim non-terminal jobs, oldest first.

        Oldest-first ordering means that when the backlog exceeds the limit, no
        job is starved. The Postgres implementation adds `FOR UPDATE SKIP
        LOCKED` so a second reconciler takes different rows.

        Args:
            limit: Maximum jobs to claim.

        Returns:
            The claimed jobs.
        """
        pending = [job for job in self._jobs.values() if not job.status.terminal]
        pending.sort(key=lambda job: job.updated_at)
        return pending[:limit]

    async def _update(self, job_id: UUID, **changes: object) -> Job:
        async with self._lock:
            job = self._jobs[job_id]
            if job.status.terminal:
                return job
            updated = job.advanced(updated_at=self.clock.now(), **changes)
            self._jobs[job_id] = updated
            return updated


@dataclass(frozen=True)
class SystemClock:
    """Wall time from the system."""

    def now(self) -> datetime:
        """Return the current UTC time.

        Returns:
            An aware UTC datetime.
        """
        from datetime import UTC

        return datetime.now(UTC)
