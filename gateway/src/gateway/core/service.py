"""The rules. No HTTP, no SQL, no RunPod vocabulary."""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import structlog

from gateway.core.models import (
    ErrorCode,
    GenerationParams,
    Job,
    JobStatus,
    RequestContext,
)
from gateway.core.protocols import (
    Clock,
    EndpointHealth,
    JobRepository,
    PromptGuardrail,
    RunPodClient,
    RunPodJobStatus,
)

logger = structlog.get_logger()

DEFAULT_JOB_DEADLINE_S = 600
DEFAULT_MAX_QUEUE_WAIT_S = 120.0
DEFAULT_AVG_JOB_S = 22.0
DEFAULT_SUBMIT_GRACE_S = 30.0
DEFAULT_HEALTH_MAX_AGE_S = 30.0
DEFAULT_MAX_ACTIVE_JOBS_PER_KEY = 10

# One lease must outlast the slowest tick. It only bounds recovery from a
# reconciler that died mid-tick; a live one releases each claim as it goes.
DEFAULT_CLAIM_LEASE_S = 60.0

RETRY_AFTER_JITTER = (0.8, 1.2)


def _retry_after_s(wait_s: float) -> int:
    """Turn an estimated wait into a jittered `Retry-After`, floored at 1s.

    The spread is not decoration: an exact wait returns every shed caller at
    the same instant, converting one queue spike into a synchronised second
    one (docs/specs/02-gateway-core.md).

    Args:
        wait_s: The estimated wait in seconds.

    Returns:
        Seconds to tell the caller to wait, at least one.
    """
    jittered = wait_s * random.uniform(*RETRY_AFTER_JITTER)  # noqa: S311 - backoff spread, not a secret
    return max(1, math.ceil(jittered))


@dataclass(frozen=True)
class Submission:
    """The outcome of one submit call.

    Attributes:
        job: The created job, or the pre-existing one on an idempotent replay.
        replayed: True when the repository returned an existing row rather than
            inserting this one. The caller cannot infer this from the job
            itself, and a retry sent with its own original trace id looks
            identical to a first attempt.
    """

    job: Job
    replayed: bool


class QueueSaturatedError(Exception):
    """Raised when the estimated queue wait exceeds the threshold.

    Attributes:
        retry_after_s: How long the caller should wait before retrying.
    """

    def __init__(self, retry_after_s: int) -> None:
        super().__init__("Estimated queue wait exceeds the threshold.")
        self.retry_after_s = retry_after_s


class ActiveJobLimitError(Exception):
    """Raised when the caller already has `max_active_jobs_per_key` unresolved.

    Bounds how much of the billable queue one key can occupy at once, so a
    single compromised or runaway credential cannot exhaust capacity for
    every other caller.

    Attributes:
        retry_after_s: How long the caller should wait before retrying.
    """

    def __init__(self, retry_after_s: int) -> None:
        super().__init__("The caller's active job limit has been reached.")
        self.retry_after_s = retry_after_s


@dataclass
class JobService:
    """Submit, read and reconcile generation jobs.

    Attributes:
        repository: Persistence.
        runpod: The upstream endpoint.
        guardrail: Prompt content check.
        clock: Injected wall time.
        job_deadline_s: Age at which an unresolved job is timed out.
        max_queue_wait_s: Estimated wait above which submissions are shed.
        avg_job_s: Expected job duration, replaced by a measured p50.
        submit_grace_s: How long a job with no upstream id is left alone before
            the reconciler may adopt it.
        health_max_age_s: Age beyond which a health reading is treated as
            unknown.
        claim_lease_s: How long a reconciler claim excludes other callers.
        max_active_jobs_per_key: Non-terminal job cap per caller.
    """

    repository: JobRepository
    runpod: RunPodClient
    guardrail: PromptGuardrail
    clock: Clock
    job_deadline_s: int = DEFAULT_JOB_DEADLINE_S
    max_queue_wait_s: float = DEFAULT_MAX_QUEUE_WAIT_S
    avg_job_s: float = DEFAULT_AVG_JOB_S
    submit_grace_s: float = DEFAULT_SUBMIT_GRACE_S
    health_max_age_s: float = DEFAULT_HEALTH_MAX_AGE_S
    claim_lease_s: float = DEFAULT_CLAIM_LEASE_S
    max_active_jobs_per_key: int = DEFAULT_MAX_ACTIVE_JOBS_PER_KEY
    _health: EndpointHealth | None = None
    _health_at: datetime | None = None
    _outstanding: int = 0

    async def submit(self, params: GenerationParams, ctx: RequestContext) -> Submission:
        """Guard, record, and dispatch a generation request.

        The guardrail runs before the insert, so a blocked prompt is still a
        recorded, attributable job rather than a bare error — and an idempotent
        replay returns that same `BLOCKED` job without re-running anything.

        The row is written before the upstream call. A crash between the two
        leaves a job with no upstream id, which `reconcile` adopts and
        resubmits once the submit grace period has passed; the reverse order
        would lose the job entirely while still being billed for it.

        Args:
            params: Validated generation parameters.
            ctx: Caller identity and trace.

        Returns:
            The created or replayed job, flagged with which it was.

        Raises:
            QueueSaturatedError: The estimated wait exceeds the threshold.
            ActiveJobLimitError: The caller is already at its active job cap.
            IdempotencyConflictError: The key was reused with a different body.
        """
        verdict = self.guardrail.check(params.prompt)
        now = self.clock.now()
        job = Job(
            id=uuid.uuid4(),
            status=JobStatus.BLOCKED if verdict.blocked else JobStatus.QUEUED,
            params=params,
            context=ctx,
            created_at=now,
            updated_at=now,
            request_hash=params.fingerprint(),
            error_code=ErrorCode.PROMPT_BLOCKED if verdict.blocked else None,
            error_message=verdict.reason if verdict.blocked else None,
            completed_at=now if verdict.blocked else None,
        )
        stored = await self.repository.create(job)

        # Identity, not status: a replay can land on a QUEUED row whose own
        # submit has not returned yet, and status alone cannot tell the two
        # apart. Submitting again there is a second billed GPU job.
        if stored.id != job.id:
            return Submission(job=stored, replayed=True)
        if stored.status is not JobStatus.QUEUED:
            return Submission(job=stored, replayed=False)

        # Shedding must never reach a replay (returned above, costs nothing
        # upstream) or a blocked prompt (also returned above). Checked here,
        # after the insert, rather than before it, precisely so a replay
        # resolves first — a caller retrying the request most likely to hit
        # the cap must get its original job id back, not a 429 it can never
        # recover the id from.
        await self._shed_if_over_capacity(stored)

        runpod_job_id = await self.runpod.submit(
            stored.params.as_worker_input(ctx.correlation_id)
        )
        logger.info(
            "job_submitted",
            job_id=str(stored.id),
            runpod_job_id=runpod_job_id,
            correlation_id=ctx.correlation_id,
            api_key_id=ctx.api_key_id,
        )
        return Submission(
            job=await self._attach(stored.id, runpod_job_id), replayed=False
        )

    async def _attach(self, job_id: UUID, runpod_job_id: str) -> Job:
        """Record the upstream id, honouring a cancel that raced the submit.

        Cancelling a job with no upstream id can only record the intent; this
        is where that intent is carried out, once there is finally something to
        cancel. Without it the GPU keeps working on a job nobody is waiting for
        and nothing holds its id.

        Args:
            job_id: The job to update.
            runpod_job_id: The upstream identifier.

        Returns:
            The updated job.
        """
        job = await self.repository.attach_runpod_id(job_id, runpod_job_id)
        if job.status is JobStatus.CANCELLED:
            await self.runpod.cancel(runpod_job_id)
            logger.info(
                "late_cancel_propagated",
                job_id=str(job_id),
                runpod_job_id=runpod_job_id,
            )
        return job

    async def get(self, job_id: UUID) -> Job | None:
        """Fetch one job.

        Args:
            job_id: The id to look up.

        Returns:
            The job, or None if unknown.
        """
        return await self.repository.get(job_id)

    async def cancel(self, job_id: UUID) -> Job | None:
        """Stop a job, upstream and locally.

        RunPod owns the queue, so only RunPod can actually stop the work and
        the billing. This asks it to, then records the outcome.

        A job already in a terminal state is returned unchanged: terminal
        states are written once, and cancelling a completed job must not
        discard its result.

        A job with no upstream id yet is cancelled locally; `_attach` issues
        the upstream cancel as soon as the in-flight submit hands back an id.

        Args:
            job_id: The job to cancel.

        Returns:
            The job, or None if unknown.
        """
        job = await self.repository.get(job_id)
        if job is None or job.status.terminal:
            return job
        if job.runpod_job_id is not None:
            await self.runpod.cancel(job.runpod_job_id)
        return await self.repository.mark_failed(
            job.id,
            ErrorCode.JOB_CANCELLED,
            "Cancelled by the caller.",
            JobStatus.CANCELLED,
        )

    async def reconcile(self, limit: int = 50) -> int:
        """Advance unresolved jobs toward a terminal state.

        Returns the number advanced. Each job is handled independently: one
        raising is logged and skipped, because a single malformed record must
        not stop every other job from ever resolving.

        Args:
            limit: Maximum jobs to process this tick.

        Returns:
            How many jobs changed state.
        """
        await self._refresh_health()
        advanced = 0
        unresolved = await self.repository.claim_unresolved(
            limit, lease_s=self.claim_lease_s, submit_grace_s=self.submit_grace_s
        )
        self._outstanding = len(unresolved)
        for job in unresolved:
            try:
                if await self._reconcile_one(job):
                    advanced += 1
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the tick
                logger.warning("reconcile_failed", job_id=str(job.id), error=str(exc))
            finally:
                # Released rather than left to expire: the lease exists to
                # survive a dead reconciler, not to slow a live one down to
                # one poll per lease.
                await self.repository.release_claim(job.id)
        return advanced

    async def _reconcile_one(self, job: Job) -> bool:
        if self._expired(job):
            return await self._time_out(job)

        if job.runpod_job_id is None:
            return await self._adopt_orphan(job)

        try:
            upstream = await self.runpod.status(job.runpod_job_id)
        except Exception as exc:  # noqa: BLE001 - unknown is not failure
            logger.info(
                "reconcile_upstream_unavailable",
                job_id=str(job.id),
                error=str(exc),
            )
            return False

        return await self._apply(job, upstream)

    async def _apply(self, job: Job, upstream: RunPodJobStatus) -> bool:
        """Write one upstream reading to the job.

        Args:
            job: The claimed job.
            upstream: What the endpoint reports.

        Returns:
            True if the job changed state.
        """
        if upstream.status is JobStatus.COMPLETED:
            return await self._complete(job, upstream)
        if upstream.status.terminal:
            await self.repository.mark_failed(
                job.id,
                upstream.error_code or ErrorCode.INFERENCE_FAILED,
                upstream.error_message or "Generation failed.",
                upstream.status,
            )
            return True
        # Only what upstream actually reports. Inferring IN_PROGRESS from a
        # local QUEUED status marks jobs running that are still in the queue,
        # which makes the queue-wait metric measure nothing.
        if upstream.status is JobStatus.IN_PROGRESS:
            await self.repository.mark_in_progress(job.id, upstream.progress)
            return True
        return False

    async def _complete(self, job: Job, upstream: RunPodJobStatus) -> bool:
        """Record a completion, or fail it when there is no image.

        Args:
            job: The claimed job.
            upstream: An upstream reading reporting COMPLETED.

        Returns:
            True, since either outcome is terminal.
        """
        if upstream.result is None or upstream.result.image_base64 is None:
            await self.repository.mark_failed(
                job.id,
                ErrorCode.INFERENCE_FAILED,
                "Upstream reported completion without an image.",
                JobStatus.FAILED,
            )
            return True
        await self.repository.mark_completed(job.id, upstream.result)
        return True

    async def _time_out(self, job: Job) -> bool:
        """Stop the upstream job, then record the deadline breach.

        The cancel comes first because the deadline says nothing about the GPU:
        without it the endpoint keeps generating, and keeps billing, an image
        the caller has already been told is timed out. A failed cancel is
        logged and does not hold up the terminal write — the endpoint's own
        execution timeout is the backstop.

        Args:
            job: The expired job.

        Returns:
            True.
        """
        if job.runpod_job_id is not None:
            try:
                await self.runpod.cancel(job.runpod_job_id)
            except Exception as exc:  # noqa: BLE001 - the timeout must still be recorded
                logger.warning(
                    "timeout_cancel_failed", job_id=str(job.id), error=str(exc)
                )
        await self.repository.mark_failed(
            job.id,
            ErrorCode.JOB_TIMEOUT,
            f"Job exceeded the {self.job_deadline_s}s deadline.",
            JobStatus.TIMED_OUT,
        )
        return True

    async def _adopt_orphan(self, job: Job) -> bool:
        """Resubmit a job recorded before its upstream call completed.

        Only reached after the submit grace period, so the absent upstream id
        is evidence that no upstream job exists to duplicate rather than
        evidence that the submit is still in flight.

        Args:
            job: The orphaned job.

        Returns:
            True if it was resubmitted.
        """
        runpod_job_id = await self.runpod.submit(
            job.params.as_worker_input(job.context.correlation_id)
        )
        await self._attach(job.id, runpod_job_id)
        logger.info(
            "orphan_resubmitted", job_id=str(job.id), runpod_job_id=runpod_job_id
        )
        return True

    def _expired(self, job: Job) -> bool:
        deadline = job.created_at + timedelta(seconds=self.job_deadline_s)
        return self.clock.now() >= deadline

    async def _refresh_health(self) -> None:
        """Cache endpoint health on the reconciler tick.

        Fetching this inside `submit` would put an upstream round trip and a
        second failure mode on the hot path for every request.
        """
        try:
            self._health = await self.runpod.health()
        except Exception as exc:  # noqa: BLE001 - stale health must not shed traffic
            logger.info("endpoint_health_unavailable", error=str(exc))
            self._health = None
            self._health_at = None
            return
        self._health_at = self.clock.now()
        logger.info(
            "endpoint_health",
            in_queue=self._health.in_queue,
            in_progress=self._health.in_progress,
            workers_running=self._health.workers_running,
            workers_idle=self._health.workers_idle,
        )

    def _check_queue_pressure(self) -> None:
        """Shed load when the estimated wait is too long.

        Fails **open** on a missing or stale reading, unlike the guardrail:
        load shedding is an optimisation, and refusing all traffic because the
        queue cannot be measured converts a monitoring failure into an outage.
        A reading only refreshes on a reconciler tick, so a dead reconciler
        would otherwise freeze one saturated snapshot and shed forever.

        Raises:
            QueueSaturatedError: The estimated wait exceeds the threshold.
        """
        if self._health is None or self._health_at is None:
            return
        if (self.clock.now() - self._health_at).total_seconds() > self.health_max_age_s:
            logger.info("endpoint_health_stale")
            return
        wait = (self._health.in_queue / self._health.capacity) * self.avg_job_s
        if wait > self.max_queue_wait_s:
            raise QueueSaturatedError(retry_after_s=_retry_after_s(wait))

    async def _shed_if_over_capacity(self, job: Job) -> None:
        """Reject a freshly inserted, not-yet-submitted job under load.

        Only ever called on a job that is already stored (see `submit`), so
        the row exists whether or not this sheds it. On shedding, the row is
        marked FAILED rather than left QUEUED with no upstream id — otherwise
        `reconcile` would later treat it as an orphaned submit and dispatch it
        anyway, which is exactly the cost shedding exists to avoid.

        The idempotency key is released with it. The 429 tells the caller to
        retry, and a key left bound to this FAILED row would replay that
        failure for the whole retention window — the retry could never become
        a real attempt. Nothing was submitted upstream, so there is no
        duplicate work to protect against.

        Args:
            job: The job just inserted, still QUEUED.

        Raises:
            QueueSaturatedError: The estimated wait exceeds the threshold.
            ActiveJobLimitError: The caller is already at its active job cap.
        """
        try:
            self._check_queue_pressure()
            await self._check_active_job_cap(job.context.api_key_id)
        except (QueueSaturatedError, ActiveJobLimitError) as exc:
            await self.repository.mark_failed(
                job.id, ErrorCode.QUEUE_SATURATED, str(exc), JobStatus.FAILED
            )
            await self.repository.release_idempotency_key(job.id)
            raise

    async def _check_active_job_cap(self, api_key_id: str) -> None:
        """Reject once the caller already has the maximum unresolved jobs.

        Counts non-terminal jobs only: a completed, failed, cancelled or
        timed-out job frees the slot immediately.

        Called after the triggering job itself has already been inserted (see
        `_shed_if_over_capacity`), so `active` counts that job too — the cap
        is exceeded once `active` is strictly greater than the max, not `>=`.

        Race note: correct today only because, for the in-memory repository,
        `count_active` never awaits and nothing between this job's insert and
        this count yields to another task — CPython's cooperative scheduler
        never interleaves them. Nothing in the `JobRepository` protocol
        guarantees that. A database-backed repository's `count_active` is a
        real I/O `await`, which reopens this as a genuine check-then-act race
        unless that repository enforces the cap itself (e.g. an atomic
        insert-and-count, or a per-key row lock).

        Args:
            api_key_id: The caller's identity.

        Raises:
            ActiveJobLimitError: The caller is over the cap.
        """
        active = await self.repository.count_active(api_key_id)
        if active > self.max_active_jobs_per_key:
            raise ActiveJobLimitError(retry_after_s=int(self.avg_job_s) + 1)

    @property
    def outstanding(self) -> int:
        """Unresolved jobs seen by the last reconcile tick.

        Returns:
            The count claimed on the most recent tick.
        """
        return self._outstanding

    @property
    def endpoint_health(self) -> EndpointHealth | None:
        """Latest cached endpoint health, for `/health/detailed`.

        Returns:
            The cached reading, or None if unavailable.
        """
        return self._health
