"""In-memory job repository.

Postgres is the specified production store (docs/specs/02-gateway-core.md);
this implements the same protocol so the service, the API and the reconciler
are fully exercised without a database. Swapping it is one binding in the
composition root.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
        retention_s: How long terminal jobs are kept. Results carry multi-MB
            base64 images, so without eviction sustained traffic grows the
            process until it OOMs.
    """

    clock: Clock
    retention_s: float = 3600.0
    _jobs: dict[UUID, Job] = field(default_factory=dict)
    _by_key: dict[tuple[str, str], UUID] = field(default_factory=dict)
    _leases: dict[UUID, datetime] = field(default_factory=dict)
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
            self._evict_expired()
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

    def _evict_expired(self) -> None:
        """Drop terminal jobs older than the retention window.

        Runs under the caller's lock. RunPod itself retains results for 30
        minutes, so an hour here already outlives the upstream copy.
        """
        cutoff = self.clock.now() - timedelta(seconds=self.retention_s)
        expired = {
            job_id
            for job_id, job in self._jobs.items()
            if job.status.terminal and job.updated_at < cutoff
        }
        if not expired:
            return
        for job_id in expired:
            del self._jobs[job_id]
            self._leases.pop(job_id, None)
        self._by_key = {
            scoped: job_id
            for scoped, job_id in self._by_key.items()
            if job_id not in expired
        }

    async def get(self, job_id: UUID) -> Job | None:
        """Fetch one job.

        Args:
            job_id: The id to look up.

        Returns:
            The job, or None if unknown.
        """
        return self._jobs.get(job_id)

    async def attach_runpod_id(self, job_id: UUID, runpod_job_id: str) -> Job:
        """Record the upstream job id, terminal status included.

        The terminal guard in `_update` deliberately does not apply: a job
        cancelled while its submit was in flight is terminal here and still
        running on a GPU, and dropping the id would leave nothing to cancel.

        Args:
            job_id: The job to update.
            runpod_job_id: The upstream identifier.

        Returns:
            The updated job.
        """
        async with self._lock:
            job = self._jobs[job_id]
            updated = job.advanced(
                runpod_job_id=runpod_job_id, updated_at=self.clock.now()
            )
            self._jobs[job_id] = updated
            return updated

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

    async def release_idempotency_key(self, job_id: UUID) -> None:
        """Drop this job's key binding, leaving the job row itself in place.

        Args:
            job_id: The job whose key binding is dropped.
        """
        async with self._lock:
            self._by_key = {
                scoped: bound
                for scoped, bound in self._by_key.items()
                if bound != job_id
            }

    async def claim_unresolved(
        self, limit: int, lease_s: float, submit_grace_s: float
    ) -> list[Job]:
        """Lease non-terminal jobs, oldest first.

        The lease is the in-memory equivalent of the Postgres implementation's
        `FOR UPDATE SKIP LOCKED`: a claimed job is invisible to other callers
        until the lease expires or `release_claim` drops it. Held under the
        lock, so two concurrent ticks cannot claim the same row.

        Oldest-first ordering means that when the backlog exceeds the limit, no
        job is starved.

        Args:
            limit: Maximum jobs to claim.
            lease_s: How long the claim excludes other callers.
            submit_grace_s: How long an id-less job is left to its submitter.

        Returns:
            The claimed jobs.
        """
        now = self.clock.now()
        async with self._lock:
            claimable = [
                job
                for job in self._jobs.values()
                if self._claimable(job, now, submit_grace_s)
            ]
            claimable.sort(key=lambda job: job.updated_at)
            claimed = claimable[:limit]
            expiry = now + timedelta(seconds=lease_s)
            for job in claimed:
                self._leases[job.id] = expiry
            return claimed

    def _claimable(self, job: Job, now: datetime, submit_grace_s: float) -> bool:
        if job.status.terminal:
            return False
        lease = self._leases.get(job.id)
        if lease is not None and lease > now:
            return False
        grace = timedelta(seconds=submit_grace_s)
        return not (job.runpod_job_id is None and now - job.created_at < grace)

    async def release_claim(self, job_id: UUID) -> None:
        """Drop a lease so the next tick can claim the job immediately.

        Args:
            job_id: The claimed job.
        """
        async with self._lock:
            self._leases.pop(job_id, None)

    async def count_active(self, api_key_id: str) -> int:
        """Count a caller's non-terminal jobs, for the per-key active cap.

        Deliberately lock-free: a plain synchronous read with no internal
        `await`. `JobService._check_active_job_cap` relies on that — it's
        what makes its check-then-act race-free under this repository. A
        real I/O-backed repository does not get that for free; see the race
        note there before reusing this shape against a database.

        Args:
            api_key_id: The caller to count.

        Returns:
            How many of that caller's jobs are not yet terminal.
        """
        return sum(
            1
            for job in self._jobs.values()
            if job.context.api_key_id == api_key_id and not job.status.terminal
        )

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
        return datetime.now(UTC)
