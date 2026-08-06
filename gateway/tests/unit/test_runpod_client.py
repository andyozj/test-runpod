"""HttpRunPodClient: retry semantics, error decoding, upstream mapping.

Uses httpx.MockTransport so every branch of the adapter runs without a
network. The transport script is a list of responses or exceptions consumed
one call at a time.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from gateway.adapters.runpod_client import (
    CircuitBreaker,
    HttpRunPodClient,
    _decode_error,
    submit_envelope_s,
)
from gateway.core.models import ErrorCode, JobStatus
from gateway.core.protocols import UpstreamUnavailableError


def scripted_client(
    script: list[httpx.Response | Exception], calls: list[httpx.Request]
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make(
    script: list[httpx.Response | Exception],
) -> tuple[HttpRunPodClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []
    return (
        HttpRunPodClient(
            endpoint_id="ep",
            api_key="k",
            client=scripted_client(script, calls),
            max_attempts=3,
        ),
        calls,
    )


async def test_submit_returns_the_upstream_id() -> None:
    client, calls = make([httpx.Response(200, json={"id": "up-1"})])

    assert await client.submit({"prompt": "x"}) == "up-1"
    assert calls[0].headers["Authorization"] == "Bearer k"


async def test_submit_does_not_retry_an_ambiguous_transport_error() -> None:
    """A read timeout after send may have created the job; never resubmit."""
    client, calls = make([httpx.ReadTimeout("read timed out")])

    with pytest.raises(UpstreamUnavailableError):
        await client.submit({"prompt": "x"})
    assert len(calls) == 1


async def test_submit_retries_a_connect_error() -> None:
    """A connect failure proves nothing was sent, so retrying is safe."""
    client, calls = make(
        [httpx.ConnectError("refused"), httpx.Response(200, json={"id": "up-2"})]
    )

    assert await client.submit({"prompt": "x"}) == "up-2"
    assert len(calls) == 2


async def test_reads_retry_on_5xx_then_succeed() -> None:
    client, calls = make(
        [httpx.Response(503), httpx.Response(200, json={"status": "IN_QUEUE"})]
    )

    result = await client.status("up-1")

    assert result.status is JobStatus.QUEUED
    assert len(calls) == 2


async def test_4xx_is_not_retried_and_resets_the_breaker() -> None:
    client, calls = make([httpx.Response(404)])
    client.breaker.record_failure(now=0.0)

    with pytest.raises(UpstreamUnavailableError):
        await client.status("up-x")
    assert len(calls) == 1
    assert client.breaker.allow(now=0.0)


async def test_completed_with_error_string_maps_to_the_envelope() -> None:
    envelope = json.dumps({"code": "PROMPT_BLOCKED", "message": "no"})
    client, _ = make(
        [httpx.Response(200, json={"status": "COMPLETED", "error": envelope})]
    )

    result = await client.status("up-1")

    assert result.status is JobStatus.BLOCKED
    assert result.error_code is ErrorCode.PROMPT_BLOCKED
    assert result.error_message == "no"


async def test_failed_without_error_reports_inference_failed() -> None:
    client, _ = make([httpx.Response(200, json={"status": "FAILED"})])

    result = await client.status("up-1")

    assert result.status is JobStatus.FAILED
    assert result.error_code is ErrorCode.INFERENCE_FAILED


async def test_completed_result_carries_the_output_fields() -> None:
    output = {
        "image_base64": "aGk=",
        "format": "png",
        "seed": 7,
        "width": 1024,
        "height": 768,
        "model_version": "m@rev",
        "timings": {"inference_s": 21.4},
    }
    client, _ = make(
        [httpx.Response(200, json={"status": "COMPLETED", "output": output})]
    )

    result = await client.status("up-1")

    assert result.result is not None
    assert result.result.seed == 7
    assert result.result.inference_seconds == 21.4


async def test_health_maps_queue_and_worker_counts() -> None:
    body = {
        "jobs": {"inQueue": 3, "inProgress": 1},
        "workers": {"running": 1, "idle": 2},
    }
    client, _ = make([httpx.Response(200, json=body)])

    health = await client.health()

    assert health.in_queue == 3
    assert health.capacity == 3


async def test_completed_without_an_image_maps_to_failed() -> None:
    """A terminal success with no image is a failure, not a result."""
    client, _ = make([httpx.Response(200, json={"status": "COMPLETED", "output": {}})])

    result = await client.status("up-1")

    assert result.status is JobStatus.FAILED
    assert result.error_code is ErrorCode.INFERENCE_FAILED
    assert result.result is None


async def test_completed_with_a_non_dict_output_maps_to_failed() -> None:
    client, _ = make(
        [httpx.Response(200, json={"status": "COMPLETED", "output": ["unexpected"]})]
    )

    result = await client.status("up-1")

    assert result.status is JobStatus.FAILED
    assert result.result is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TIMED_OUT", JobStatus.TIMED_OUT),
        ("CANCELLED", JobStatus.CANCELLED),
    ],
)
async def test_terminal_upstream_statuses_are_preserved(
    raw: str, expected: JobStatus
) -> None:
    envelope = json.dumps({"code": "INFERENCE_FAILED", "message": "stopped"})
    client, _ = make([httpx.Response(200, json={"status": raw, "error": envelope})])

    result = await client.status("up-1")

    assert result.status is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TIMED_OUT", ErrorCode.JOB_TIMEOUT),
        ("CANCELLED", ErrorCode.JOB_CANCELLED),
    ],
)
async def test_terminal_upstream_statuses_carry_their_own_code(
    raw: str, expected: ErrorCode
) -> None:
    client, _ = make([httpx.Response(200, json={"status": raw})])

    result = await client.status("up-1")

    assert result.error_code is expected


async def test_a_non_json_body_raises_upstream_unavailable() -> None:
    client, _ = make([httpx.Response(200, text="<html>gateway timeout</html>")])

    with pytest.raises(UpstreamUnavailableError):
        await client.status("up-1")


async def test_a_non_object_json_body_raises_upstream_unavailable() -> None:
    client, _ = make([httpx.Response(200, json=["unexpected"])])

    with pytest.raises(UpstreamUnavailableError):
        await client.status("up-1")


async def test_submit_without_an_id_raises_upstream_unavailable() -> None:
    client, _ = make([httpx.Response(200, json={"status": "IN_QUEUE"})])

    with pytest.raises(UpstreamUnavailableError):
        await client.submit({"prompt": "x"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"code": "OOM", "message": "vram"}, {"code": "OOM", "message": "vram"}),
        (
            json.dumps({"code": "OOM", "message": "vram"}),
            {"code": "OOM", "message": "vram"},
        ),
        ("plain text failure", {"message": "plain text failure"}),
        (json.dumps(["not", "a", "dict"]), {"message": '["not", "a", "dict"]'}),
    ],
)
def test_decode_error_normalises_every_shape(
    raw: Any, expected: dict[str, Any]
) -> None:
    assert _decode_error(raw) == expected


@pytest.mark.parametrize(
    "escape", [asyncio.CancelledError(), RuntimeError("client is closed")]
)
async def test_a_probe_escaping_without_a_verdict_frees_the_permit(
    escape: BaseException,
) -> None:
    """A disconnect mid-probe once wedged the breaker shut for the process."""
    scripted: list[BaseException | httpx.Response] = [
        escape,
        httpx.Response(200, json={"status": "IN_QUEUE"}),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        step = scripted.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    client = HttpRunPodClient(
        endpoint_id="ep",
        api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        breaker=CircuitBreaker(threshold=1, cooldown_s=0.0),
    )
    client.breaker.record_failure(now=0.0)

    with pytest.raises(type(escape)):
        await client.status("up-1")

    assert (await client.status("up-1")).status is JobStatus.QUEUED


# --- The breaker as `_request` actually drives it ---


def with_breaker(
    script: list[httpx.Response | Exception], breaker: CircuitBreaker
) -> tuple[HttpRunPodClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []
    return (
        HttpRunPodClient(
            endpoint_id="ep",
            api_key="k",
            client=scripted_client(script, calls),
            max_attempts=3,
            breaker=breaker,
        ),
        calls,
    )


async def test_exhausted_retries_raise_and_open_the_breaker() -> None:
    """The unit tests drive the breaker directly; this proves `_request` does."""
    breaker = CircuitBreaker(threshold=3, cooldown_s=30.0)
    client, calls = with_breaker([httpx.ConnectError("refused")] * 3, breaker)

    with pytest.raises(UpstreamUnavailableError):
        await client.status("up-1")

    assert len(calls) == 3
    assert not breaker.allow(now=asyncio.get_running_loop().time())


async def test_an_open_breaker_fast_fails_without_touching_the_transport() -> None:
    """Spending a full retry cycle on a host known to be down is the whole cost."""
    breaker = CircuitBreaker(threshold=1, cooldown_s=3600.0)
    client, calls = with_breaker([httpx.Response(200, json={"id": "up-1"})], breaker)
    breaker.record_failure(now=asyncio.get_running_loop().time())

    with pytest.raises(UpstreamUnavailableError, match="circuit breaker open"):
        await client.status("up-1")

    assert calls == []


async def test_a_success_after_the_cooldown_closes_the_breaker_again() -> None:
    breaker = CircuitBreaker(threshold=1, cooldown_s=0.0)
    client, calls = with_breaker(
        [httpx.Response(200, json={"status": "IN_QUEUE"})], breaker
    )
    breaker.record_failure(now=asyncio.get_running_loop().time())

    assert (await client.status("up-1")).status is JobStatus.QUEUED
    assert len(calls) == 1
    assert breaker.allow(now=asyncio.get_running_loop().time())


# --- Real-transport URL paths and partial output ---


async def test_cancel_posts_to_the_cancel_path() -> None:
    client, calls = make([httpx.Response(200, json={"status": "CANCELLED"})])

    await client.cancel("up-7")

    assert calls[0].method == "POST"
    assert calls[0].url.path == "/v2/ep/cancel/up-7"


async def test_status_gets_the_status_path() -> None:
    client, calls = make([httpx.Response(200, json={"status": "IN_QUEUE"})])

    await client.status("up-7")

    assert calls[0].method == "GET"
    assert calls[0].url.path == "/v2/ep/status/up-7"


async def test_submit_posts_the_input_envelope_to_the_run_path() -> None:
    client, calls = make([httpx.Response(200, json={"id": "up-1"})])

    await client.submit({"prompt": "x"})

    assert calls[0].url.path == "/v2/ep/run"
    assert json.loads(calls[0].content) == {"input": {"prompt": "x"}}


async def test_health_gets_the_health_path() -> None:
    client, calls = make([httpx.Response(200, json={})])

    await client.health()

    assert calls[0].url.path == "/v2/ep/health"


async def test_in_progress_partial_output_is_parsed_as_progress() -> None:
    """The only signal a caller gets while the GPU is still working."""
    output = {"step": 7, "total": 28, "percent": 25}
    client, _ = make(
        [httpx.Response(200, json={"status": "IN_PROGRESS", "output": output})]
    )

    result = await client.status("up-1")

    assert result.status is JobStatus.IN_PROGRESS
    assert result.progress is not None
    assert (result.progress.step, result.progress.total, result.progress.percent) == (
        7,
        28,
        25,
    )
    assert result.result is None


@pytest.mark.parametrize(
    "output",
    [
        None,
        {},
        {"step": 7},
        ["not", "a", "dict"],
        "a string",
    ],
    ids=["absent", "empty", "no-total", "list", "string"],
)
async def test_in_progress_without_a_usable_total_reports_no_progress(
    output: Any,
) -> None:
    """`total` is what makes a percentage meaningful; without it, report nothing."""
    body: dict[str, Any] = {"status": "IN_PROGRESS"}
    if output is not None:
        body["output"] = output
    client, _ = make([httpx.Response(200, json=body)])

    result = await client.status("up-1")

    assert result.status is JobStatus.IN_PROGRESS
    assert result.progress is None


async def test_a_queued_job_reports_no_progress() -> None:
    client, _ = make([httpx.Response(200, json={"status": "IN_QUEUE"})])

    result = await client.status("up-1")

    assert result.status is JobStatus.QUEUED
    assert result.progress is None


async def test_an_unknown_upstream_status_raises_rather_than_guessing() -> None:
    client, _ = make([httpx.Response(200, json={"status": "SOMETHING_NEW"})])

    with pytest.raises(ValueError, match="unknown RunPod status"):
        await client.status("up-1")


def test_the_submit_envelope_counts_every_attempt_and_every_sleep() -> None:
    assert submit_envelope_s(1, 30.0) == 30.0
    assert submit_envelope_s(3, 30.0) == pytest.approx(3 * 30.0 + 0.2 + 0.4)
    assert submit_envelope_s(3, 30.0) > 3 * 30.0
