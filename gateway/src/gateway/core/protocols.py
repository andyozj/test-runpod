"""The seams. Implementations live in `adapters/`; `core/` knows only these."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from gateway.core.metrics import CompletedTiming
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


class UpstreamUnavailableError(Exception):
    """Raised when the endpoint cannot be reached or the breaker is open."""


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


@dataclass(frozen=True)
class StoredImage:
    """Where a decoded image landed, and its placeholder.

    Attributes:
        path: Store-relative location, kept on the job row.
        thumbhash: Base64 ThumbHash (~25 bytes) computed from the decoded
            pixels, enough to render a placeholder without the file.
    """

    path: str
    thumbhash: str


@dataclass(frozen=True)
class RateLimitDecision:
    """Whether one request may proceed, and if not, when to retry.

    Attributes:
        allowed: True when a token was taken and the request may proceed.
        retry_after_s: Seconds until a token will be available, ceiled and
            floored at one. Zero when `allowed`.
    """

    allowed: bool
    retry_after_s: int


@runtime_checkable
class RateLimiter(Protocol):
    """A per-caller request budget, consulted once per first-attempt submit."""

    def acquire(self, api_key_id: str) -> RateLimitDecision:
        """Spend one token from the caller's budget, if one exists.

        Args:
            api_key_id: The caller's identity; each caller has its own budget.

        Returns:
            The decision, carrying the wait when the budget is exhausted.
        """
        ...


@runtime_checkable
class ImageStore(Protocol):
    """File storage for decoded result images, so they leave the job row."""

    async def save(
        self, job_id: UUID, image_base64: str, image_format: str
    ) -> StoredImage:
        """Decode and persist one result image.

        Args:
            job_id: The owning job; names the file.
            image_base64: The worker's encoded image.
            image_format: `png` or `jpeg`; names the extension.

        Returns:
            The stored location and its ThumbHash.
        """
        ...

    async def load(self, path: str) -> bytes | None:
        """Read a stored image back.

        Args:
            path: The store-relative path from `StoredImage.path`.

        Returns:
            The image bytes, or None when the file has been evicted.
        """
        ...


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
        """Record the upstream job id, whatever the job's status.

        Recording an id is not a state transition, so it applies to terminal
        jobs too: a job cancelled while its submit was in flight still has GPU
        work running, and the id is the only handle on it.

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

    async def release_idempotency_key(self, job_id: UUID) -> None:
        """Unbind a job's idempotency key so the same key can be used again.

        Only for a job the gateway itself refused before doing any work. A key
        stays bound for the whole retention window otherwise — that binding is
        what makes a replay return the original job instead of billing a
        second GPU run.

        Args:
            job_id: The job whose key binding is dropped. A job with no key,
                or an unknown id, is a no-op.
        """
        ...

    async def claim_unresolved(
        self, limit: int, lease_s: float, submit_grace_s: float
    ) -> list[Job]:
        """Lease non-terminal jobs for reconciliation, oldest first.

        A claim is exclusive for `lease_s`, so a tick overlapping the previous
        one — or a second reconciler — takes different rows. The lease expires
        rather than being held, so a caller that dies mid-tick does not strand
        its jobs.

        A job with no upstream id younger than `submit_grace_s` is not claimed:
        its row is written before the submit returns, and adopting it inside
        that window submits the same job twice.

        Args:
            limit: Maximum jobs to claim.
            lease_s: How long the claim excludes other callers.
            submit_grace_s: How long an id-less job is left to its submitter.

        Returns:
            The claimed jobs.
        """
        ...

    async def release_claim(self, job_id: UUID) -> None:
        """Drop a lease so the next tick can claim the job immediately.

        Args:
            job_id: The claimed job.
        """
        ...

    async def count_active(self, api_key_id: str) -> int:
        """Count a caller's non-terminal jobs, for the per-key active cap.

        Args:
            api_key_id: The caller to count.

        Returns:
            How many of that caller's jobs are not yet terminal.
        """
        ...

    async def list_recent(self, api_key_id: str, limit: int) -> list[Job]:
        """List one caller's jobs, newest first.

        Args:
            api_key_id: The caller whose jobs are listed.
            limit: Maximum jobs to return.

        Returns:
            Up to `limit` jobs, newest `created_at` first.
        """
        ...

    async def count_by_status(self, api_key_id: str) -> dict[JobStatus, int]:
        """Count one caller's jobs per status, over everything in the store.

        Args:
            api_key_id: The caller whose jobs are counted.

        Returns:
            A count for every `JobStatus`, zero where the caller has none.
        """
        ...

    async def count_created_since(self, api_key_id: str, since: datetime) -> int:
        """Count one caller's jobs created at or after an instant.

        Args:
            api_key_id: The caller whose jobs are counted.
            since: Inclusive lower bound on `created_at`.

        Returns:
            How many of the caller's jobs were created in the interval.
        """
        ...

    async def recent_completed_timings(
        self, api_key_id: str, limit: int
    ) -> list[CompletedTiming]:
        """Fetch timings of the caller's most recently completed jobs.

        Timings only, never full rows: the metrics window needs three numbers
        per job, and a completed row carries a multi-MB result.

        Args:
            api_key_id: The caller whose completions are read.
            limit: Maximum timings to return.

        Returns:
            Up to `limit` timings, newest `completed_at` first.
        """
        ...

    async def count_completed_by_hour(
        self, api_key_id: str, since: datetime
    ) -> dict[datetime, int]:
        """Count the caller's completions per UTC hour.

        Args:
            api_key_id: The caller whose completions are counted.
            since: Inclusive lower bound on `completed_at`.

        Returns:
            Completions keyed by UTC hour start; hours with none are absent.
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

    async def cancel(self, runpod_job_id: str) -> None:
        """Stop a queued or running job upstream.

        Args:
            runpod_job_id: The upstream identifier.
        """
        ...


class GuardrailVerdict(Protocol):
    """What `core/` needs from a guardrail's answer, and nothing more.

    Structural rather than a shared class: the adapter owns the concrete
    verdict, including whatever else it carries (categories, a score), and
    `core/` must not grow a dependency on that shape to read two fields.

    Read-only members, so a frozen dataclass satisfies it.
    """

    @property
    def blocked(self) -> bool:
        """Whether the request must not proceed."""
        ...

    @property
    def reason(self) -> str | None:
        """Human-readable explanation, safe to log and to return."""
        ...


@runtime_checkable
class PromptGuardrail(Protocol):
    """A content check applied before any GPU time is spent."""

    def check(self, prompt: str) -> GuardrailVerdict:
        """Classify a prompt.

        Args:
            prompt: The raw text.

        Returns:
            The verdict for this prompt.
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
    "CompletedTiming",
    "EndpointHealth",
    "GenerationParams",
    "GuardrailVerdict",
    "IdempotencyConflictError",
    "ImageStore",
    "JobRepository",
    "PromptGuardrail",
    "RateLimitDecision",
    "RateLimiter",
    "RunPodClient",
    "RunPodJobStatus",
    "StoredImage",
    "UpstreamUnavailableError",
]
