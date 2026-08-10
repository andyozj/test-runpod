"""FastAPI application. A thin translation layer over `JobService`."""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from fastapi import (
    APIRouter,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from gateway.api.schemas import (
    ErrorBody,
    ErrorResponse,
    GenerationRequest,
    JobCreated,
    JobList,
    JobSummary,
    JobView,
    MetricsView,
    QueueHealthView,
    ReconcilerView,
    UpstreamView,
)
from gateway.core.models import ErrorCode, GenerationParams, Job, RequestContext
from gateway.core.protocols import (
    IdempotencyConflictError,
    ImageStore,
    UpstreamUnavailableError,
)
from gateway.core.service import (
    ActiveJobLimitError,
    JobService,
    QueueSaturatedError,
    RateLimitedError,
    Submission,
)
from gateway.settings import Settings

logger = structlog.get_logger()

# A tick normally lands every `reconcile_interval_s` to
# `reconcile_idle_interval_s`; this many idle intervals of silence means the
# loop is dead, not slow.
RECONCILER_STALL_INTERVALS = 3


def reconciler_stall_s(settings: Settings) -> float:
    """Silence after which the reconciler is treated as stalled.

    Derived from the configured idle interval rather than fixed, so slowing
    the loop down does not turn every reading into a false stall.

    Args:
        settings: Runtime configuration.

    Returns:
        The threshold in seconds (30.0 with the default 10s idle interval).
    """
    return settings.reconcile_idle_interval_s * RECONCILER_STALL_INTERVALS


@dataclass
class Deps:
    """What the routes need. Assembled by the composition root.

    Attributes:
        service: The domain service.
        settings: Runtime configuration.
        image_store: Where offloaded result images are read from. None in
            memory mode, where results stay inline on the job.
        reconciler_age: Seconds since the reconciler last completed a tick,
            None before its first run. Absent outside the composition root.
    """

    service: JobService
    settings: Settings
    image_store: ImageStore | None = None
    reconciler_age: Callable[[], float | None] | None = None


def _error(
    status_code: int, code: ErrorCode, message: str, suggestion: str | None = None
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=ErrorBody(
            code=code.value, message=message, suggestion=suggestion
        ).model_dump(),
    )


def _shed(message: str, retry_after_s: int, correlation_id: str) -> HTTPException:
    """A 429 telling the caller to retry later.

    Shared by queue saturation, the per-key active job cap and the per-key
    request rate limit: all three are QUEUE_SATURATED to the caller — "shed
    this request, try again shortly" — and the contract has no more specific
    code for the cap or the limit (no RATE_LIMITED exists).

    Args:
        message: Caller-facing description of what was shed.
        retry_after_s: How long to wait before retrying.
        correlation_id: The request's trace id.

    Returns:
        The exception to raise.
    """
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=ErrorBody(
            code=ErrorCode.QUEUE_SATURATED.value,
            message=message,
            suggestion=f"Retry after {retry_after_s}s.",
            correlation_id=correlation_id,
        ).model_dump(),
        headers={"Retry-After": str(retry_after_s)},
    )


def authenticate(settings: Settings, authorization: str | None) -> str:
    """Resolve the caller behind a bearer token.

    Args:
        settings: Holds the configured keys.
        authorization: The raw Authorization header.

    Returns:
        The resolved `api_key_id`.

    Raises:
        HTTPException: The header is absent, malformed, or the key is unknown.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.UNAUTHENTICATED,
            "Missing or malformed Authorization header.",
            "Send `Authorization: Bearer <api-key>`.",
        )
    key_id = settings.resolve_key(authorization.removeprefix("Bearer "))
    if key_id is None:
        logger.warning("auth_failed")
        raise _error(
            status.HTTP_401_UNAUTHORIZED, ErrorCode.UNAUTHENTICATED, "Invalid API key."
        )
    return key_id


async def _submit(
    service: JobService, params: GenerationParams, ctx: RequestContext
) -> Submission:
    """Submit through the service, translating domain errors to HTTP ones.

    Split out of the route handler because inlining it pushed `build_router`
    past the complexity budget — the error branches belong to the submit
    step, not to routing.

    Args:
        service: The domain service.
        params: Validated generation parameters.
        ctx: Caller identity and trace.

    Returns:
        The created or replayed job.

    Raises:
        HTTPException: Translated from the service's domain errors.
    """
    try:
        return await service.submit(params, ctx)
    except UpstreamUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorBody(
                code=ErrorCode.UPSTREAM_UNAVAILABLE.value,
                message="The endpoint is unreachable.",
                suggestion="Retry with the same Idempotency-Key.",
                correlation_id=ctx.correlation_id,
            ).model_dump(),
            headers={"Retry-After": "5"},
        ) from exc
    except RateLimitedError as exc:
        raise _shed(
            "Too many requests for this key.", exc.retry_after_s, ctx.correlation_id
        ) from exc
    except QueueSaturatedError as exc:
        raise _shed(
            "The endpoint queue is saturated.", exc.retry_after_s, ctx.correlation_id
        ) from exc
    except ActiveJobLimitError as exc:
        raise _shed(
            "You have reached your active job limit.",
            exc.retry_after_s,
            ctx.correlation_id,
        ) from exc
    except IdempotencyConflictError as exc:
        raise _error(
            status.HTTP_409_CONFLICT,
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "Idempotency-Key was reused with a different request body.",
            "Use a new key, or resend the original body.",
        ) from exc


def _not_found(job_id: UUID) -> HTTPException:
    return _error(
        status.HTTP_404_NOT_FOUND,
        ErrorCode.JOB_NOT_FOUND,
        f"No job with id {job_id}.",
    )


async def _image_bytes(job: Job, store: ImageStore | None) -> tuple[bytes, str]:
    """Fetch a job's image bytes, wherever they live.

    Args:
        job: The owner-checked job.
        store: The bound image store, if any.

    Returns:
        The image bytes and their format.

    Raises:
        HTTPException: 404 when the job never produced an image, 410 when it
            did and the file has since been evicted.
    """
    result = job.result
    if result is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.JOB_NOT_FOUND,
            f"Job {job.id} has no image.",
        )
    if result.image_path is not None and store is not None:
        data = await store.load(result.image_path)
        if data is not None:
            return data, result.format
    elif result.image_base64 is not None:
        try:
            return base64.b64decode(result.image_base64), result.format
        except binascii.Error:
            logger.warning("stored_image_undecodable", job_id=str(job.id))
    # There is no GONE code in the error contract; JOB_NOT_FOUND plus the 410
    # status carries the distinction.
    raise _error(
        status.HTTP_410_GONE,
        ErrorCode.JOB_NOT_FOUND,
        f"The image for job {job.id} was evicted from the store.",
        "The job's metadata and seed remain; rerun the seed to regenerate it.",
    )


def _upstream_view(deps: Deps) -> UpstreamView:
    """Assemble the shared topology block, mirroring `/health/detailed`.

    Args:
        deps: The assembled dependencies.

    Returns:
        Queue health with its staleness, plus reconciler liveness. Both carry
        the gateway's own stale/fresh verdict and the threshold behind it, so
        no caller has to invent a cutoff.
    """
    return UpstreamView(
        queue=QueueHealthView.of(
            deps.service.endpoint_health,
            deps.service.endpoint_health_age_s,
            stale_after_s=deps.service.health_max_age_s,
        ),
        reconciler=ReconcilerView.of(
            deps.reconciler_age() if deps.reconciler_age else None,
            stale_after_s=reconciler_stall_s(deps.settings),
        ),
    )


def _register_metrics(router: APIRouter, deps: Deps) -> None:
    """Register the metrics route.

    Split out of `build_router` for the same reason as `_submit`: the extra
    nested handler pushed it past the complexity budget.

    Args:
        router: The versioned router.
        deps: The assembled dependencies.
    """

    @router.get("/metrics")
    async def get_metrics(
        authorization: str | None = Header(default=None),
    ) -> MetricsView:
        """Serve the Operate dashboard's aggregates.

        Job counts, latency percentiles, throughput and cost are per-key —
        the caller sees only its own jobs, consistent with the job routes.
        The upstream block is shared topology, the same reading
        `/health/detailed` reports and equally behind auth.

        Args:
            authorization: Bearer credential.

        Returns:
            The caller's aggregates plus the shared upstream readings.
        """
        key_id = authenticate(deps.settings, authorization)
        snapshot = await deps.service.metrics(key_id)
        return MetricsView.of(snapshot, _upstream_view(deps))


def build_router(deps: Deps) -> APIRouter:
    """Create the versioned routes bound to a set of dependencies.

    Args:
        deps: The assembled dependencies.

    Returns:
        The router.
    """
    router = APIRouter(prefix="/v1")
    _register_metrics(router, deps)

    def _authenticate(authorization: str | None) -> str:
        return authenticate(deps.settings, authorization)

    @router.post("/jobs", status_code=status.HTTP_202_ACCEPTED, response_model=None)
    async def create_job(
        body: GenerationRequest,
        response: Response,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
        x_correlation_id: str | None = Header(default=None),
    ) -> JobCreated | JobView:
        """Submit a generation job.

        Args:
            body: Generation parameters.
            response: Injected so a replay can be answered with 200.
            authorization: Bearer credential.
            idempotency_key: Optional key enabling safe retries.
            x_correlation_id: Optional trace id; generated when absent.

        Returns:
            The created job, or the original job on an idempotent replay.
        """
        key_id = _authenticate(authorization)
        ctx = RequestContext(
            api_key_id=key_id,
            correlation_id=x_correlation_id or str(uuid.uuid4()),
            idempotency_key=idempotency_key,
        )
        submission = await _submit(deps.service, body.to_params(), ctx)

        job = submission.job
        response.headers["X-Correlation-ID"] = ctx.correlation_id
        if submission.replayed:
            # The repository is the only thing that knows: a client retrying
            # with its own original correlation id is still a replay.
            response.status_code = status.HTTP_200_OK
            response.headers["Idempotency-Replayed"] = "true"
            return JobView.of(job)
        return JobCreated(
            job_id=job.id, status=job.status.value, created_at=job.created_at
        )

    @router.get("/jobs/{job_id}")
    async def get_job(
        job_id: UUID, authorization: str | None = Header(default=None)
    ) -> JobView:
        """Fetch job status, progress and result.

        Args:
            job_id: The job to read.
            authorization: Bearer credential.

        Returns:
            The job's current state.
        """
        key_id = _authenticate(authorization)
        job = await deps.service.get(job_id)
        # Another caller's job answers 404, not 403: confirming the id exists
        # is itself a leak.
        if job is None or job.context.api_key_id != key_id:
            raise _not_found(job_id)
        return JobView.of(job)

    @router.get("/jobs")
    async def list_jobs(
        authorization: str | None = Header(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> JobList:
        """List the caller's jobs, newest first: the gallery feed.

        Metadata only — thumbhash and image URL included, image bytes never.

        Args:
            authorization: Bearer credential.
            limit: Maximum jobs to return.

        Returns:
            Up to `limit` of the caller's own jobs.
        """
        key_id = _authenticate(authorization)
        jobs = await deps.service.list_jobs(key_id, limit)
        return JobList(jobs=[JobSummary.of(job) for job in jobs])

    @router.get("/jobs/{job_id}/image")
    async def get_job_image(
        job_id: UUID, authorization: str | None = Header(default=None)
    ) -> Response:
        """Serve a job's image.

        Args:
            job_id: The job whose image is fetched.
            authorization: Bearer credential.

        Returns:
            The image bytes with their content type. 404 for another caller's
            or an image-less job, 410 once the file has been evicted.
        """
        key_id = _authenticate(authorization)
        job = await deps.service.get(job_id)
        if job is None or job.context.api_key_id != key_id:
            raise _not_found(job_id)
        data, image_format = await _image_bytes(job, deps.image_store)
        return Response(content=data, media_type=f"image/{image_format}")

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(
        job_id: UUID, authorization: str | None = Header(default=None)
    ) -> JobView:
        """Stop a queued or running job.

        Delegates to RunPod's own cancel operation rather than reimplementing
        it — the platform owns the queue, so it is the only thing that can
        actually stop the work and stop the billing.

        Args:
            job_id: The job to cancel.
            authorization: Bearer credential.

        Returns:
            The job in its cancelled state.
        """
        key_id = _authenticate(authorization)
        existing = await deps.service.get(job_id)
        if existing is None or existing.context.api_key_id != key_id:
            raise _not_found(job_id)
        job = await deps.service.cancel(job_id)
        if job is None:
            raise _not_found(job_id)
        return JobView.of(job)

    return router


def create_app(
    deps: Deps,
    on_startup: Callable[[], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """Assemble the application.

    Args:
        deps: The assembled dependencies.
        on_startup: Optional async context manager for background tasks.

    Returns:
        The configured application.
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if on_startup is None:
            yield
            return
        async with on_startup():
            yield

    app = FastAPI(
        title="FLUX.1-dev gateway",
        version=deps.settings.version,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def correlate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        response = await call_next(request)
        response.headers.setdefault("X-Correlation-ID", correlation_id)
        return response

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        # Starlette types `detail` as Any; `object` is enough to narrow it and
        # keeps the Any out of the module.
        detail: object = exc.detail
        body = (
            ErrorBody(**detail)
            if isinstance(detail, dict)
            else ErrorBody(code="ERROR", message=str(detail))
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(error=body).model_dump(),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default 422 is remapped so every error a caller sees has
        # the same envelope.
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"][1:]) or "body"
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=ErrorResponse(
                error=ErrorBody(
                    code=_code_for(field).value,
                    message=f"{field}: {first['msg']}",
                    suggestion="Correct the field and resubmit.",
                )
            ).model_dump(),
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness. Unauthenticated, no I/O.

        Probes generally cannot hold a credential, and requiring one turns
        liveness checking into a credential-distribution problem. It reveals
        only that the process is up.

        Returns:
            Status and version.
        """
        return {"status": "ok", "version": deps.settings.version}

    @app.get("/health/detailed")
    async def health_detailed(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Dependency status. Authenticated, because it reports topology.

        Args:
            authorization: Bearer credential.

        Returns:
            Per-dependency status. `Any`: a heterogeneous JSON document —
            strings at the top level, nested per-check objects of
            str/int/float/None. Naming that union buys no checking, since
            FastAPI serialises from the annotation and the shape is asserted
            by the tests.
        """
        authenticate(deps.settings, authorization)
        upstream = deps.service.endpoint_health
        age = deps.reconciler_age() if deps.reconciler_age else None
        stalled = age is not None and age > reconciler_stall_s(deps.settings)
        return {
            "status": "ok" if upstream and not stalled else "degraded",
            "version": deps.settings.version,
            "checks": {
                "runpod": (
                    {
                        "status": "ok",
                        "in_queue": upstream.in_queue,
                        "in_progress": upstream.in_progress,
                        "workers_running": upstream.workers_running,
                        "workers_idle": upstream.workers_idle,
                    }
                    if upstream
                    else {"status": "unknown", "detail": "no health reading yet"}
                ),
                "reconciler": (
                    {"status": "stalled" if stalled else "ok", "last_tick_s": age}
                    if age is not None
                    else {"status": "unknown", "detail": "no completed tick yet"}
                ),
            },
        }

    app.include_router(build_router(deps))
    return app


def _code_for(field: str) -> ErrorCode:
    if field in {"width", "height"}:
        return ErrorCode.INVALID_DIMENSIONS
    if field == "num_inference_steps":
        return ErrorCode.INVALID_STEPS
    return ErrorCode.INVALID_PROMPT
