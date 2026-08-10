"""Wire schemas. Validation happens here, at the boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gateway.core.metrics import LatencyStats, MetricsSnapshot
from gateway.core.models import GenerationParams, Job, Progress
from gateway.core.protocols import EndpointHealth

MIN_DIMENSION = 256
MAX_DIMENSION = 1536
MAX_PROMPT_CHARS = 2000


class GenerationRequest(BaseModel):
    """Incoming generation parameters. Only `prompt` is required.

    Mirrors `contracts/generation-request.schema.json`.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "prompt": "a red fox in falling snow, cinematic lighting",
                "width": 1024,
                "height": 1024,
                "num_inference_steps": 28,
            }
        },
    )

    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    width: int = Field(default=1024, ge=MIN_DIMENSION, le=MAX_DIMENSION)
    height: int = Field(default=1024, ge=MIN_DIMENSION, le=MAX_DIMENSION)
    num_inference_steps: int = Field(default=28, ge=1, le=50)
    guidance_scale: float = Field(default=3.5, ge=0, le=20)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    output_format: Literal["png", "jpeg"] = "png"

    @field_validator("prompt")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            msg = "prompt must not be blank"
            raise ValueError(msg)
        return value

    def to_params(self) -> GenerationParams:
        """Convert to the domain type.

        Returns:
            The equivalent `GenerationParams`.
        """
        return GenerationParams(**self.model_dump())


class JobCreated(BaseModel):
    """Response to a successful submission."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
                "status": "QUEUED",
                "created_at": "2026-08-06T02:39:23Z",
            }
        }
    )

    job_id: UUID
    status: str
    created_at: datetime


class ErrorBody(BaseModel):
    """The error envelope every failure shares."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": "QUEUE_SATURATED",
                "message": "You have reached your active job limit.",
                "suggestion": "Retry after 22s.",
                "correlation_id": "9f1c2b7e-2d5a-4a1e-9c0b-7a3f8e11d240",
            }
        }
    )

    code: str
    message: str
    suggestion: str | None = None
    correlation_id: str | None = None


class ErrorResponse(BaseModel):
    """Wrapper so errors are distinguishable from results at a glance."""

    error: ErrorBody


def image_url_of(job: Job) -> str:
    """Return the image route for a job.

    Args:
        job: The job whose image is addressed.

    Returns:
        The versioned path a client fetches the image from.
    """
    return f"/v1/jobs/{job.id}/image"


def _progress_view(progress: Progress | None) -> dict[str, int | str] | None:
    """Render live progress for the wire.

    Preview keys appear only on a stride that actually carried a frame, so a
    preview-less update keeps the original three-field shape.

    Args:
        progress: The job's latest progress, if any.

    Returns:
        The progress document, or None while there is none.
    """
    if progress is None:
        return None
    view: dict[str, int | str] = {
        "step": progress.step,
        "total": progress.total,
        "percent": progress.percent,
    }
    if progress.preview_b64 is not None:
        view["preview_b64"] = progress.preview_b64
        view["preview_format"] = progress.preview_format or "jpeg"
    return view


def _request_view(params: GenerationParams) -> dict[str, Any]:
    """Render the submitted parameters for the wire.

    Everything needed to reproduce the job — steps and guidance included,
    which the result alone does not carry. `seed` appears only when the
    caller fixed one; the seed actually used is on the result.

    Args:
        params: The stored request parameters.

    Returns:
        The request document.
    """
    view: dict[str, Any] = {
        "prompt": params.prompt,
        "width": params.width,
        "height": params.height,
        "num_inference_steps": params.num_inference_steps,
        "guidance_scale": params.guidance_scale,
        "output_format": params.output_format,
    }
    if params.seed is not None:
        view["seed"] = params.seed
    return view


def _result_view(job: Job) -> dict[str, Any] | None:
    """Render a job's result for the wire.

    An offloaded image (postgres mode) is referenced by `image_url` plus its
    `thumbhash`; an inline one (memory mode) keeps the original
    `image_base64` shape.

    Args:
        job: The job whose result is rendered.

    Returns:
        The result document, or None while there is none.
    """
    result = job.result
    if result is None:
        return None
    view: dict[str, Any] = {
        "format": result.format,
        "seed": result.seed,
        "width": result.width,
        "height": result.height,
        "model_version": result.model_version,
        "inference_seconds": result.inference_seconds,
    }
    if result.image_path is not None:
        view["image_url"] = image_url_of(job)
        view["thumbhash"] = result.thumbhash
    else:
        view["image_base64"] = result.image_base64
    return view


class JobView(BaseModel):
    """Full job state.

    One shape with three populated states — running, completed, failed — so a
    client parses one response type regardless of outcome.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
                "status": "COMPLETED",
                "request": {
                    "prompt": "a red fox in falling snow, cinematic lighting",
                    "width": 1024,
                    "height": 1024,
                    "num_inference_steps": 28,
                    "guidance_scale": 3.5,
                    "output_format": "png",
                },
                "progress": {"step": 28, "total": 28, "percent": 100},
                "result": {
                    "image_url": (
                        "/v1/jobs/3f2504e0-4f89-11d3-9a0c-0305e82c3301/image"
                    ),
                    "thumbhash": "4PcJNZqAh4eAd3d3iHiHiICAB/iH",
                    "format": "png",
                    "seed": 918273,
                    "width": 1024,
                    "height": 1024,
                    "model_version": (
                        "black-forest-labs/FLUX.1-dev@"
                        "3de623fc3c33e44ffbe2bad470d0f45bccf2eb21"
                    ),
                    "inference_seconds": 21.4,
                },
                "error": None,
                "created_at": "2026-08-06T02:39:23Z",
                "updated_at": "2026-08-06T02:39:48Z",
                "completed_at": "2026-08-06T02:39:48Z",
            }
        }
    )

    job_id: UUID
    status: str
    request: dict[str, Any]
    progress: dict[str, int | str] | None = None
    result: dict[str, Any] | None = None
    error: ErrorBody | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @classmethod
    def of(cls, job: Job) -> JobView:
        """Render a domain job for the wire.

        Args:
            job: The job to render.

        Returns:
            The wire representation.
        """
        return cls(
            job_id=job.id,
            status=job.status.value,
            request=_request_view(job.params),
            progress=_progress_view(job.progress),
            result=_result_view(job),
            error=(
                ErrorBody(
                    code=job.error_code.value,
                    message=job.error_message or "",
                    correlation_id=job.context.correlation_id,
                )
                if job.error_code
                else None
            ),
            created_at=job.created_at,
            updated_at=job.updated_at,
            completed_at=job.completed_at,
        )


class JobSummary(BaseModel):
    """One gallery row: metadata only, never image bytes."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
                "status": "COMPLETED",
                "prompt": "a red fox in falling snow, cinematic lighting",
                "width": 1024,
                "height": 1024,
                "num_inference_steps": 28,
                "guidance_scale": 3.5,
                "output_format": "png",
                "seed": 918273,
                "thumbhash": "4PcJNZqAh4eAd3d3iHiHiICAB/iH",
                "image_url": "/v1/jobs/3f2504e0-4f89-11d3-9a0c-0305e82c3301/image",
                "model_version": (
                    "black-forest-labs/FLUX.1-dev@"
                    "3de623fc3c33e44ffbe2bad470d0f45bccf2eb21"
                ),
                "inference_seconds": 21.4,
                "error_code": None,
                "created_at": "2026-08-06T02:39:23Z",
                "completed_at": "2026-08-06T02:39:48Z",
            }
        }
    )

    job_id: UUID
    status: str
    prompt: str
    width: int
    height: int
    num_inference_steps: int
    guidance_scale: float
    output_format: str
    seed: int | None = None
    thumbhash: str | None = None
    image_url: str | None = None
    model_version: str | None = None
    inference_seconds: float | None = None
    error_code: str | None = None
    created_at: datetime
    completed_at: datetime | None = None

    @classmethod
    def of(cls, job: Job) -> JobSummary:
        """Render a domain job as a gallery row.

        Dimensions and seed prefer the result (what was actually rendered)
        over the request. Steps, guidance and output format are request
        knobs with no result counterpart, so they come from the stored
        params. `image_url` is present whenever a result exists; an evicted
        image answers 410 there rather than vanishing here.

        Args:
            job: The job to render.

        Returns:
            The wire representation, image bytes excluded.
        """
        result = job.result
        return cls(
            job_id=job.id,
            status=job.status.value,
            prompt=job.params.prompt,
            width=result.width if result else job.params.width,
            height=result.height if result else job.params.height,
            num_inference_steps=job.params.num_inference_steps,
            guidance_scale=job.params.guidance_scale,
            output_format=job.params.output_format,
            seed=result.seed if result else job.params.seed,
            thumbhash=result.thumbhash if result else None,
            image_url=image_url_of(job) if result else None,
            model_version=result.model_version if result else None,
            inference_seconds=result.inference_seconds if result else None,
            error_code=job.error_code.value if job.error_code else None,
            created_at=job.created_at,
            completed_at=job.completed_at,
        )


class JobList(BaseModel):
    """Response to the gallery listing."""

    jobs: list[JobSummary]


class JobCounts(BaseModel):
    """The caller's job totals."""

    by_status: dict[str, int]
    last_hour: int
    active_now: int


class LatencyPercentiles(BaseModel):
    """Nearest-rank percentiles over one latency series, seconds."""

    p50_s: float | None = None
    p95_s: float | None = None
    max_s: float | None = None

    @classmethod
    def of(cls, stats: LatencyStats) -> LatencyPercentiles:
        """Render one core latency summary for the wire.

        Args:
            stats: The computed percentiles.

        Returns:
            The wire representation.
        """
        return cls(p50_s=stats.p50_s, p95_s=stats.p95_s, max_s=stats.max_s)


class LatencyView(BaseModel):
    """Latency percentiles over the window, two series."""

    inference: LatencyPercentiles
    wall: LatencyPercentiles


class ThroughputBucket(BaseModel):
    """Completions in one UTC hour."""

    hour: datetime
    completed: int


class CostView(BaseModel):
    """Estimated GPU spend over the window. Estimates, never billing data.

    `exec_seconds_in_window` is the input the estimate was computed from, so
    `exec_seconds_in_window * gpu_rate_usd_hr / 3600 == estimated_cost_usd`
    holds and the number can be checked rather than trusted.
    """

    estimated_cost_usd: float
    estimated_cost_usd_per_job: float | None = None
    exec_seconds_in_window: float
    gpu_rate_usd_hr: float


class QueueHealthView(BaseModel):
    """The cached upstream queue reading, with its age and trustworthiness.

    `stale` is the gateway's own judgement, made against the same threshold
    load shedding uses (`HEALTH_MAX_AGE_S`): true when the reading is older
    than `stale_after_s` or absent entirely, in both cases meaning the counts
    must not be read as current.
    """

    status: str
    in_queue: int | None = None
    in_progress: int | None = None
    workers_running: int | None = None
    workers_idle: int | None = None
    age_s: float | None = None
    stale: bool
    stale_after_s: float

    @classmethod
    def of(
        cls,
        health: EndpointHealth | None,
        age_s: float | None,
        stale_after_s: float,
    ) -> QueueHealthView:
        """Render the cached queue reading, `unknown` when there is none.

        Args:
            health: The cached reading, if any.
            age_s: Seconds since it was taken.
            stale_after_s: Age beyond which the reading is not to be trusted.

        Returns:
            The wire representation.
        """
        if health is None or age_s is None:
            return cls(status="unknown", stale=True, stale_after_s=stale_after_s)
        return cls(
            status="ok",
            in_queue=health.in_queue,
            in_progress=health.in_progress,
            workers_running=health.workers_running,
            workers_idle=health.workers_idle,
            age_s=age_s,
            stale=age_s > stale_after_s,
            stale_after_s=stale_after_s,
        )


class ReconcilerView(BaseModel):
    """Reconciler liveness: `ok`, `stalled`, or `unknown` before a tick.

    `stale` is `status == "stalled"` widened to cover `unknown`: true whenever
    the loop is not known to have ticked within `stale_after_s`, which is
    derived from the configured idle tick interval.
    """

    status: str
    last_tick_s: float | None = None
    stale: bool
    stale_after_s: float

    @classmethod
    def of(cls, last_tick_s: float | None, stale_after_s: float) -> ReconcilerView:
        """Render reconciler liveness, `unknown` before the first tick.

        Args:
            last_tick_s: Seconds since the last completed tick, if any.
            stale_after_s: Silence beyond which the loop is treated as dead.

        Returns:
            The wire representation.
        """
        if last_tick_s is None:
            return cls(status="unknown", stale=True, stale_after_s=stale_after_s)
        stalled = last_tick_s > stale_after_s
        return cls(
            status="stalled" if stalled else "ok",
            last_tick_s=last_tick_s,
            stale=stalled,
            stale_after_s=stale_after_s,
        )


class UpstreamView(BaseModel):
    """Shared topology: queue health and reconciler liveness, not per-key."""

    queue: QueueHealthView
    reconciler: ReconcilerView


class MetricsView(BaseModel):
    """The Operate dashboard's data source: one caller's aggregates."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "jobs": {
                    "by_status": {
                        "QUEUED": 1,
                        "IN_PROGRESS": 2,
                        "COMPLETED": 155,
                        "FAILED": 3,
                        "TIMED_OUT": 1,
                        "CANCELLED": 2,
                        "BLOCKED": 4,
                    },
                    "last_hour": 12,
                    "active_now": 3,
                },
                "latency": {
                    "inference": {"p50_s": 14.3, "p95_s": 22.1, "max_s": 38.9},
                    "wall": {"p50_s": 19.8, "p95_s": 41.5, "max_s": 93.2},
                },
                "throughput": [
                    {"hour": "2026-08-08T13:00:00Z", "completed": 0},
                    {"hour": "2026-08-08T14:00:00Z", "completed": 7},
                    {"hour": "2026-08-09T12:00:00Z", "completed": 12},
                ],
                "cost": {
                    "estimated_cost_usd": 0.7243,
                    "estimated_cost_usd_per_job": 0.0072,
                    "exec_seconds_in_window": 1490.0,
                    "gpu_rate_usd_hr": 1.75,
                },
                "upstream": {
                    "queue": {
                        "status": "ok",
                        "in_queue": 2,
                        "in_progress": 1,
                        "workers_running": 1,
                        "workers_idle": 0,
                        "age_s": 4.2,
                        "stale": False,
                        "stale_after_s": 30.0,
                    },
                    "reconciler": {
                        "status": "ok",
                        "last_tick_s": 1.9,
                        "stale": False,
                        "stale_after_s": 30.0,
                    },
                },
                "window": 100,
                "completed_in_window": 100,
                "window_started_at": "2026-08-09T08:12:03Z",
                "window_ended_at": "2026-08-09T12:31:44Z",
                "generated_at": "2026-08-09T12:34:56Z",
            }
        }
    )

    jobs: JobCounts
    latency: LatencyView
    throughput: list[ThroughputBucket]
    cost: CostView
    upstream: UpstreamView
    window: int
    completed_in_window: int
    window_started_at: datetime | None = None
    window_ended_at: datetime | None = None
    generated_at: datetime

    @classmethod
    def of(cls, snapshot: MetricsSnapshot, upstream: UpstreamView) -> MetricsView:
        """Render a core metrics snapshot for the wire.

        Args:
            snapshot: The caller-scoped aggregates.
            upstream: The shared queue and reconciler readings.

        Returns:
            The wire representation.
        """
        return cls(
            jobs=JobCounts(
                by_status={
                    status.value: count for status, count in snapshot.by_status.items()
                },
                last_hour=snapshot.created_last_hour,
                active_now=snapshot.active_now,
            ),
            latency=LatencyView(
                inference=LatencyPercentiles.of(snapshot.inference),
                wall=LatencyPercentiles.of(snapshot.wall),
            ),
            throughput=[
                ThroughputBucket(hour=bucket.hour, completed=bucket.completed)
                for bucket in snapshot.throughput
            ],
            cost=CostView(
                estimated_cost_usd=snapshot.estimated_cost_usd,
                estimated_cost_usd_per_job=snapshot.estimated_cost_usd_per_job,
                exec_seconds_in_window=snapshot.exec_seconds_in_window,
                gpu_rate_usd_hr=snapshot.gpu_rate_usd_hr,
            ),
            upstream=upstream,
            window=snapshot.window,
            completed_in_window=snapshot.completed_in_window,
            window_started_at=snapshot.window_started_at,
            window_ended_at=snapshot.window_ended_at,
            generated_at=snapshot.generated_at,
        )
