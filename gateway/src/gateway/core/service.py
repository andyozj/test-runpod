"""The rules. No HTTP, no SQL, no RunPod vocabulary."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
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
)

logger = structlog.get_logger()

DEFAULT_JOB_DEADLINE_S = 600
DEFAULT_MAX_QUEUE_WAIT_S = 120.0
DEFAULT_AVG_JOB_S = 22.0


class QueueSaturatedError(Exception):
    """Raised when the estimated queue wait exceeds the threshold.

    Attributes:
        retry_after_s: How long the caller should wait before retrying.
    """

    def __init__(self, retry_after_s: int) -> None:
        super().__init__("Estimated queue wait exceeds the threshold.")
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
    """

    repository: JobRepository
    runpod: RunPodClient
    guardrail: PromptGuardrail
    clock: Clock
    job_deadline_s: int = DEFAULT_JOB_DEADLINE_S
    max_queue_wait_s: float = DEFAULT_MAX_QUEUE_WAIT_S
    avg_job_s: float = DEFAULT_AVG_JOB_S
    _health: EndpointHealth | None = None

    async def submit(self, params: GenerationParams, ctx: RequestContext) -> Job:
        """Guard, record, and dispatch a generation request.

        The guardrail runs before the insert, so a blocked prompt is still a
        recorded, attributable job rather than a bare error — and an idempotent
        replay returns that same `BLOCKED` job without re-running anything.

        The row is written before the upstream call. A crash between the two
        leaves a job with no upstream id, which `reconcile` adopts and
        resubmits; the reverse order would lose the job entirely while still
        being billed for it.

        Args:
            params: Validated generation parameters.
            ctx: Caller identity and trace.

        Returns:
            The created or replayed job.

        Raises:
            QueueSaturatedError: The estimated wait exceeds the threshold.
            IdempotencyConflictError: The key was reused with a different body.
        """
        self._check_queue_pressure()

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
            error_message=getattr(verdict, "reason", None) if verdict.blocked else None,
            completed_at=now if verdict.blocked else None,
        )
        stored = await self.repository.create(job)

        if stored.status is not JobStatus.QUEUED or stored.runpod_job_id is not None:
            return stored

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
        return await self.repository.attach_runpod_id(stored.id, runpod_job_id)

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
            ErrorCode.JOB_TIMEOUT,
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
        for job in await self.repository.claim_unresolved(limit):
            try:
                if await self._reconcile_one(job):
                    advanced += 1
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the tick
                logger.warning("reconcile_failed", job_id=str(job.id), error=str(exc))
        return advanced

    async def _reconcile_one(self, job: Job) -> bool:
        if self._expired(job):
            await self.repository.mark_failed(
                job.id,
                ErrorCode.JOB_TIMEOUT,
                f"Job exceeded the {self.job_deadline_s}s deadline.",
                JobStatus.TIMED_OUT,
            )
            return True

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

        if upstream.status is JobStatus.COMPLETED and upstream.result is not None:
            await self.repository.mark_completed(job.id, upstream.result)
            return True
        if upstream.status.terminal:
            await self.repository.mark_failed(
                job.id,
                upstream.error_code or ErrorCode.INFERENCE_FAILED,
                upstream.error_message or "Generation failed.",
                upstream.status,
            )
            return True
        if upstream.progress is not None or job.status is JobStatus.QUEUED:
            await self.repository.mark_in_progress(job.id, upstream.progress)
            return True
        return False

    async def _adopt_orphan(self, job: Job) -> bool:
        """Resubmit a job recorded before its upstream call completed.

        Safe because the absent upstream id is precisely the evidence that no
        upstream job exists to duplicate.

        Args:
            job: The orphaned job.

        Returns:
            True if it was resubmitted.
        """
        runpod_job_id = await self.runpod.submit(
            job.params.as_worker_input(job.context.correlation_id)
        )
        await self.repository.attach_runpod_id(job.id, runpod_job_id)
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
            return
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

        Raises:
            QueueSaturatedError: The estimated wait exceeds the threshold.
        """
        if self._health is None:
            return
        wait = (self._health.in_queue / self._health.capacity) * self.avg_job_s
        if wait > self.max_queue_wait_s:
            raise QueueSaturatedError(retry_after_s=int(wait) + 1)

    @property
    def endpoint_health(self) -> EndpointHealth | None:
        """Latest cached endpoint health, for `/health/detailed`.

        Returns:
            The cached reading, or None if unavailable.
        """
        return self._health
