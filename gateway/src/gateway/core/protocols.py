"""The seams. Implementations live in `adapters/`; `core/` knows only these."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from gateway.core.models import (
    ErrorCode,
    GenerationParams,
    Job,
    JobResult,
    JobStatus,
    Progress,
)


class IdempotencyConflictError(Exception):
    """Raised when a key is replayed with a different request body."""


@dataclass(frozen=True)
class RunPodJobStatus:
    """Upstream job state, already mapped out of RunPod's vocabulary.

    Attributes:
        status: The corresponding domain status.
        result: Populated on completion.
        progress: Populated while running, when the worker reports it.
        error_code: Populated on failure.
        error_message: Populated on failure.
    """

    status: JobStatus
    result: JobResult | None = None
    progress: Progress | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class EndpointHealth:
    """Queue and worker counts from the RunPod endpoint.

    Attributes:
        in_queue: Jobs waiting.
        in_progress: Jobs running.
        workers_running: Workers currently executing.
        workers_idle: Workers warm and available.
    """

    in_queue: int
    in_progress: int
    workers_running: int
    workers_idle: int

    @property
    def capacity(self) -> int:
        """Workers that could take a job, floored at one.

        Counts running workers as available, which they are not until their
        current job finishes — so any wait derived from this is a lower bound.

        Returns:
            The worker count, never below one.

        Example:
            >>> EndpointHealth(0, 0, 0, 0).capacity
            1
            >>> EndpointHealth(4, 1, 1, 2).capacity
            3
        """
        return max(self.workers_running + self.workers_idle, 1)


@runtime_checkable
class JobRepository(Protocol):
    """Persistence for jobs. Named transitions, never a generic update."""

    async def create(self, job: Job) -> Job:
        """Insert a job, enforcing idempotency.

        Args:
            job: The job to store.

        Returns:
            The stored job, or the pre-existing one when the idempotency key
            has been seen with the same request body.

        Raises:
            IdempotencyConflictError: The key was reused with a different body.
        """
        ...

    async def get(self, job_id: UUID) -> Job | None:
        """Fetch one job.

        Args:
            job_id: The id to look up.

        Returns:
            The job, or None if unknown.
        """
        ...

    async def attach_runpod_id(self, job_id: UUID, runpod_job_id: str) -> Job:
        """Record the upstream job id.

        Args:
            job_id: The job to update.
            runpod_job_id: The upstream identifier.

        Returns:
            The updated job.
        """
        ...

    async def mark_in_progress(self, job_id: UUID, progress: Progress | None) -> Job:
        """Advance a job to running, optionally storing progress.

        Args:
            job_id: The job to update.
            progress: Latest progress, if reported.

        Returns:
            The updated job.
        """
        ...

    async def mark_completed(self, job_id: UUID, result: JobResult) -> Job:
        """Record a successful result.

        Args:
            job_id: The job to update.
            result: What the worker produced.

        Returns:
            The updated job.
        """
        ...

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
        ...

    async def claim_unresolved(self, limit: int) -> list[Job]:
        """Claim non-terminal jobs for reconciliation, oldest first.

        Args:
            limit: Maximum jobs to claim.

        Returns:
            The claimed jobs.
        """
        ...


@runtime_checkable
class RunPodClient(Protocol):
    """The upstream serverless endpoint."""

    async def submit(self, payload: dict[str, Any]) -> str:
        """Submit a job.

        Args:
            payload: The worker input.

        Returns:
            The upstream job id.
        """
        ...

    async def status(self, runpod_job_id: str) -> RunPodJobStatus:
        """Fetch upstream job state.

        Args:
            runpod_job_id: The upstream identifier.

        Returns:
            The mapped status.
        """
        ...

    async def health(self) -> EndpointHealth:
        """Fetch queue and worker counts.

        Returns:
            The endpoint's current health.
        """
        ...


@runtime_checkable
class PromptGuardrail(Protocol):
    """A content check applied before any GPU time is spent."""

    def check(self, prompt: str) -> Any:
        """Classify a prompt.

        Args:
            prompt: The raw text.

        Returns:
            A verdict exposing `blocked`, `categories` and `reason`.
        """
        ...


@runtime_checkable
class Clock(Protocol):
    """Wall time, injected so timestamps are assertable."""

    def now(self) -> datetime:
        """Return the current time.

        Returns:
            An aware UTC datetime.
        """
        ...


__all__ = [
    "Clock",
    "EndpointHealth",
    "GenerationParams",
    "IdempotencyConflictError",
    "JobRepository",
    "PromptGuardrail",
    "RunPodClient",
    "RunPodJobStatus",
]
