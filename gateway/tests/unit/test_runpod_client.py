"""HttpRunPodClient: retry semantics, error decoding, upstream mapping.

Uses httpx.MockTransport so every branch of the adapter runs without a
network. The transport script is a list of responses or exceptions consumed
one call at a time.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from gateway.adapters.runpod_client import HttpRunPodClient, _decode_error
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
