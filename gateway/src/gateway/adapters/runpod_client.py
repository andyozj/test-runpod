"""HTTP client for the RunPod serverless endpoint.

Owns every upstream failure concern — retry, breaker, timeouts, and the
mapping out of RunPod's vocabulary — so `core/` never sees a transport detail.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from gateway.core.models import ErrorCode, JobResult, JobStatus, Progress
from gateway.core.protocols import EndpointHealth, RunPodJobStatus

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


class UpstreamUnavailableError(Exception):
    """Raised when the endpoint cannot be reached or the breaker is open."""


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

    def allow(self, now: float) -> bool:
        """Whether a call may proceed.

        Args:
            now: Monotonic time.

        Returns:
            True when closed, or when half-open for a single probe.
        """
        if self._opened_at is None:
            return True
        return now - self._opened_at >= self.cooldown_s

    def record_success(self) -> None:
        """Reset after a successful call."""
        if self._opened_at is not None:
            logger.info("breaker_closed")
        self._failures = 0
        self._opened_at = None

    def record_failure(self, now: float) -> None:
        """Count a failure and open once the threshold is reached.

        Args:
            now: Monotonic time.
        """
        self._failures += 1
        if self._failures >= self.threshold and self._opened_at is None:
            self._opened_at = now
            logger.warning("breaker_opened", failures=self._failures)


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
        """
        body = await self._request("POST", "run", json={"input": payload})
        return str(body["id"])

    async def status(self, runpod_job_id: str) -> RunPodJobStatus:
        """Fetch and map upstream job state.

        Args:
            runpod_job_id: The upstream identifier.

        Returns:
            The mapped status, with result, progress or error populated.
        """
        body = await self._request("GET", f"status/{runpod_job_id}")
        status = map_status(str(body.get("status", "")))
        output = body.get("output") or {}

        if status is JobStatus.COMPLETED:
            if isinstance(output, dict) and "error" in output:
                return _error_status(output["error"])
            return RunPodJobStatus(status=status, result=_result(output))
        if status.terminal:
            return RunPodJobStatus(
                status=status,
                error_code=ErrorCode.INFERENCE_FAILED,
                error_message=str(body.get("error", "Generation failed.")),
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

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
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
                # so retrying only wastes time.
                self.breaker.record_success()
                raise UpstreamUnavailableError(str(exc)) from exc
            except (httpx.HTTPError, UpstreamUnavailableError) as exc:
                last = exc
                self.breaker.record_failure(loop.time())
                if attempt < self.max_attempts:
                    await asyncio.sleep(_backoff(attempt))
                    logger.info("upstream_retry", attempt=attempt, error=str(exc))
                continue
            else:
                self.breaker.record_success()
                body: dict[str, Any] = response.json()
                return body

        raise UpstreamUnavailableError(str(last))


def _backoff(attempt: int) -> float:
    """Exponential backoff with full jitter.

    Args:
        attempt: 1-based attempt number.

    Returns:
        Seconds to sleep.
    """
    return random.uniform(0, min(2.0**attempt * 0.1, 2.0))  # noqa: S311


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


def _error_status(error: dict[str, Any]) -> RunPodJobStatus:
    raw = str(error.get("code", ErrorCode.INFERENCE_FAILED.value))
    try:
        code = ErrorCode(raw)
    except ValueError:
        code = ErrorCode.INFERENCE_FAILED
    status = JobStatus.BLOCKED if raw.endswith("_BLOCKED") else JobStatus.FAILED
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
