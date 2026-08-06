"""HTTP client for the RunPod serverless endpoint.

Owns every upstream failure concern — retry, breaker, timeouts, and the
mapping out of RunPod's vocabulary — so `core/` never sees a transport detail.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from gateway.core.models import ErrorCode, JobResult, JobStatus, Progress
from gateway.core.protocols import (
    EndpointHealth,
    RunPodJobStatus,
    UpstreamUnavailableError,
)

logger = structlog.get_logger()

BASE_URL = "https://api.runpod.ai/v2"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# RunPod vocabulary -> domain. An unknown value raises rather than defaulting:
# silently mapping it to something plausible is how a job ends up in the wrong
# terminal state.
_STATUS_MAP = {
    "IN_QUEUE": JobStatus.QUEUED,
    "IN_PROGRESS": JobStatus.IN_PROGRESS,
    "COMPLETED": JobStatus.COMPLETED,
    "FAILED": JobStatus.FAILED,
    "CANCELLED": JobStatus.CANCELLED,
    "TIMED_OUT": JobStatus.TIMED_OUT,
}

_TERMINAL_CODE = {
    JobStatus.TIMED_OUT: ErrorCode.JOB_TIMEOUT,
    JobStatus.CANCELLED: ErrorCode.JOB_CANCELLED,
}


def map_status(raw: str) -> JobStatus:
    """Translate a RunPod status into the domain enum.

    Args:
        raw: The upstream status string.

    Returns:
        The corresponding domain status.

    Raises:
        ValueError: The status is not recognised.

    Example:
        >>> map_status("IN_QUEUE")
        <JobStatus.QUEUED: 'QUEUED'>
    """
    try:
        return _STATUS_MAP[raw]
    except KeyError as exc:
        msg = f"unknown RunPod status: {raw!r}"
        raise ValueError(msg) from exc


@dataclass
class CircuitBreaker:
    """Fails fast while the upstream is known to be down.

    Without one, an outage turns every request into a full retry cycle and the
    gateway spends its capacity waiting on a dead host.

    Attributes:
        threshold: Consecutive failures before opening.
        cooldown_s: How long to stay open before probing.
    """

    threshold: int = 5
    cooldown_s: float = 30.0
    _failures: int = 0
    _opened_at: float | None = None
    _probing: bool = False

    def allow(self, now: float) -> bool:
        """Take permission for a call, admitting one probe per cooldown.

        Mutating, not a query: admitting the probe is the decision. Letting
        every waiting caller through the moment the cooldown elapses re-hammers
        a host that has not been shown to be back yet, which is the stampede
        the breaker exists to prevent.

        Args:
            now: Monotonic time.

        Returns:
            True when closed, or for the single probe that reopens it.
        """
        if self._opened_at is None:
            return True
        if self._probing or now - self._opened_at < self.cooldown_s:
            return False
        self._probing = True
        return True

    def record_success(self) -> None:
        """Reset after a successful call."""
        if self._opened_at is not None:
            logger.info("breaker_closed")
        self._failures = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self, now: float) -> None:
        """Count a failure and open once the threshold is reached.

        Args:
            now: Monotonic time.
        """
        self._failures += 1
        self._probing = False
        if self._failures >= self.threshold:
            # Re-stamping on every failure past the threshold restarts the
            # cooldown, so a failed half-open probe re-opens the breaker
            # instead of leaving it permanently closed mid-outage.
            if self._opened_at is None:
                logger.warning("breaker_opened", failures=self._failures)
            self._opened_at = now


@dataclass
class HttpRunPodClient:
    """Talks to one RunPod serverless endpoint.

    Attributes:
        endpoint_id: The endpoint to call.
        api_key: RunPod API key.
        client: Injected httpx client, so tests supply a transport.
        max_attempts: Total attempts per call, including the first.
        breaker: Shared circuit breaker.
    """

    endpoint_id: str
    api_key: str
    client: httpx.AsyncClient
    max_attempts: int = 3
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    async def submit(self, payload: dict[str, Any]) -> str:
        """Submit a job.

        Args:
            payload: The worker input.

        Returns:
            The upstream job id.

        Raises:
            UpstreamUnavailableError: The response carries no job id.
        """
        body = await self._request(
            "POST", "run", json={"input": payload}, idempotent=False
        )
        runpod_job_id = body.get("id")
        if runpod_job_id is None:
            msg = "accepted response carries no job id"
            raise UpstreamUnavailableError(msg)
        return str(runpod_job_id)

    async def status(self, runpod_job_id: str) -> RunPodJobStatus:
        """Fetch and map upstream job state.

        Args:
            runpod_job_id: The upstream identifier.

        Returns:
            The mapped status, with result, progress or error populated. A
            COMPLETED reading carrying no image is mapped to FAILED: a terminal
            success with nothing in it is a failure the caller cannot act on.
        """
        body = await self._request("GET", f"status/{runpod_job_id}")
        status = map_status(str(body.get("status", "")))
        output = body.get("output") or {}

        raw_error = body.get(
            "error", output.get("error") if isinstance(output, dict) else None
        )

        if status is JobStatus.COMPLETED:
            if raw_error is not None:
                return _error_status(_decode_error(raw_error), JobStatus.FAILED)
            return _completion(output)
        if status.terminal:
            if raw_error is not None:
                return _error_status(_decode_error(raw_error), status)
            return RunPodJobStatus(
                status=status,
                error_code=_TERMINAL_CODE.get(status, ErrorCode.INFERENCE_FAILED),
                error_message="Generation failed.",
            )
        return RunPodJobStatus(status=status, progress=_progress(output))

    async def health(self) -> EndpointHealth:
        """Fetch queue and worker counts.

        Returns:
            The endpoint's current health.
        """
        body = await self._request("GET", "health")
        jobs = body.get("jobs", {})
        workers = body.get("workers", {})
        return EndpointHealth(
            in_queue=int(jobs.get("inQueue", 0)),
            in_progress=int(jobs.get("inProgress", 0)),
            workers_running=int(workers.get("running", 0)),
            workers_idle=int(workers.get("idle", 0)),
        )

    async def cancel(self, runpod_job_id: str) -> None:
        """Stop a queued or running job.

        Args:
            runpod_job_id: The upstream identifier.
        """
        await self._request("POST", f"cancel/{runpod_job_id}")

    async def _request(
        self, method: str, path: str, *, idempotent: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        if not self.breaker.allow(loop.time()):
            raise UpstreamUnavailableError("circuit breaker open")

        url = f"{BASE_URL}/{self.endpoint_id}/{path}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self.client.request(
                    method, url, headers=headers, **kwargs
                )
                if response.status_code in RETRYABLE_STATUS:
                    msg = f"upstream {response.status_code}"
                    raise UpstreamUnavailableError(msg)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # 4xx other than 429: the payload is rejected on every attempt,
                # so retrying only wastes time. The breaker resets because a
                # rejection proves the host is up and answering.
                self.breaker.record_success()
                raise UpstreamUnavailableError(str(exc)) from exc
            except (httpx.HTTPError, UpstreamUnavailableError) as exc:
                self.breaker.record_failure(loop.time())
                # A transport error after the request was sent (read timeout,
                # dropped connection) is ambiguous: the job may already exist
                # upstream. Retrying a non-idempotent call there creates a
                # duplicate GPU job, so only connect-phase failures and
                # explicit retryable statuses are retried for those.
                ambiguous = isinstance(exc, httpx.HTTPError) and not isinstance(
                    exc, httpx.ConnectError | httpx.ConnectTimeout
                )
                if not idempotent and ambiguous:
                    raise UpstreamUnavailableError(str(exc)) from exc
                last = exc
                if attempt < self.max_attempts:
                    await asyncio.sleep(_backoff(attempt))
                    logger.info("upstream_retry", attempt=attempt, error=str(exc))
                continue
            else:
                self.breaker.record_success()
                return _body(response, path)

        raise UpstreamUnavailableError(str(last))


def _backoff(attempt: int) -> float:
    """Exponential backoff with full jitter.

    Args:
        attempt: 1-based attempt number.

    Returns:
        Seconds to sleep.
    """
    return random.uniform(0, min(2.0**attempt * 0.1, 2.0))  # noqa: S311


def _body(response: httpx.Response, path: str) -> dict[str, Any]:
    """Parse a 2xx body, treating anything unparseable as unavailability.

    An HTML error page from an intermediary is a 200 with a body we cannot
    read; surfacing it as a `JSONDecodeError` puts a transport failure into
    call sites that only handle `UpstreamUnavailableError`.

    Args:
        response: The successful response.
        path: The endpoint path, for the error message.

    Returns:
        The decoded object.

    Raises:
        UpstreamUnavailableError: The body is not a JSON object.
    """
    try:
        # Any: the decoded shape is whatever the upstream sent, and narrowing
        # it to a dict is exactly what the check below does.
        body: Any = response.json()
    except ValueError as exc:
        msg = f"non-JSON response from {path}"
        raise UpstreamUnavailableError(msg) from exc
    if not isinstance(body, dict):
        msg = f"unexpected {type(body).__name__} body from {path}"
        raise UpstreamUnavailableError(msg)
    return body


def _completion(output: Any) -> RunPodJobStatus:
    """Map a COMPLETED reading, demoting an imageless one to FAILED.

    Args:
        output: The raw `output` field, of any shape.

    Returns:
        The completion, or an INFERENCE_FAILED status when no image is present.
    """
    if not isinstance(output, dict) or not output.get("image_base64"):
        return RunPodJobStatus(
            status=JobStatus.FAILED,
            error_code=ErrorCode.INFERENCE_FAILED,
            error_message="Upstream reported completion without an image.",
        )
    return RunPodJobStatus(status=JobStatus.COMPLETED, result=_result(output))


def _result(output: dict[str, Any]) -> JobResult:
    return JobResult(
        image_base64=output.get("image_base64"),
        format=str(output.get("format", "png")),
        seed=int(output.get("seed", 0)),
        width=int(output.get("width", 0)),
        height=int(output.get("height", 0)),
        model_version=str(output.get("model_version", "unknown")),
        inference_seconds=float(output.get("timings", {}).get("inference_s", 0.0)),
    )


def _decode_error(raw: Any) -> dict[str, Any]:
    """Normalise the upstream error field into an envelope dict.

    The worker JSON-encodes its envelope because the platform drops dict
    errors. Anything undecodable becomes a message-only envelope rather than
    an exception — a malformed error must never mask the failure it reports.
    """
    if isinstance(raw, dict):
        return raw
    try:
        decoded = json.loads(str(raw))
    except (TypeError, ValueError):
        return {"message": str(raw)}
    return decoded if isinstance(decoded, dict) else {"message": str(raw)}


def _error_status(error: dict[str, Any], status: JobStatus) -> RunPodJobStatus:
    """Build a failure reading, keeping the upstream's own terminal status.

    Args:
        error: The decoded error envelope.
        status: The mapped upstream status. TIMED_OUT and CANCELLED are
            distinct outcomes and are preserved; collapsing them into FAILED
            loses the only signal that says whether a deadline or a caller
            stopped the job.

    Returns:
        The mapped failure.
    """
    raw = str(error.get("code", ErrorCode.INFERENCE_FAILED.value))
    try:
        code = ErrorCode(raw)
    except ValueError:
        code = ErrorCode.INFERENCE_FAILED
    if raw.endswith("_BLOCKED"):
        status = JobStatus.BLOCKED
    return RunPodJobStatus(
        status=status,
        error_code=code,
        error_message=str(error.get("message", "Generation failed.")),
    )


def _progress(output: Any) -> Progress | None:
    if not isinstance(output, dict) or "total" not in output:
        return None
    return Progress(
        step=int(output.get("step", 0)),
        total=int(output["total"]),
        percent=int(output.get("percent", 0)),
    )
