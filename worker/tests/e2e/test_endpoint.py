"""E2E against a live endpoint. Deselected by default; run with `-m gpu`.

Written before the endpoint exists so deploy-day verification is one command:

    export RUNPOD_API_KEY=... RUNPOD_ENDPOINT_ID=...
    cd worker && uv run pytest -m gpu tests/e2e -v

Each test maps to a case in docs/specs/07-testing.md. Cases owned by the
gateway (idempotency replay) or requiring orchestration (cold-start
decomposition) live elsewhere: the benchmark harness owns timing.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
from typing import Any

import pytest

from worker.contracts import contract_path

pytestmark = pytest.mark.gpu

RUN_TIMEOUT_S = 300
POLL_INTERVAL_S = 1.0


def _endpoint() -> Any:
    import runpod

    api_key = os.environ.get("RUNPOD_API_KEY")
    endpoint_id = os.environ.get("RUNPOD_ENDPOINT_ID")
    if not api_key or not endpoint_id:
        msg = "RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID not set"
        if os.environ.get("CI"):
            # In CI this suite is a required post-deploy gate, not an
            # optional extra — a missing secret must fail the workflow,
            # not silently pass by skipping every test.
            pytest.fail(msg)
        pytest.skip(msg)
    runpod.api_key = api_key
    return runpod.Endpoint(endpoint_id)


def _run_and_wait(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the terminal job record: status, plus output or error.

    The SDK pops an `error` key out of the handler's return, promotes it to
    the job's `error` field and marks the job FAILED — so error envelopes are
    never in `output`, and `job.output()` is None for them.
    """
    job = _endpoint().run(payload)
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while time.monotonic() < deadline:
        status = job.status()
        if status in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            record: dict[str, Any] = job._fetch_job()  # noqa: SLF001 - no public accessor returns error + output together
            return record
        time.sleep(POLL_INTERVAL_S)
    msg = f"job not terminal within {RUN_TIMEOUT_S}s"
    raise TimeoutError(msg)


def _error_envelope(record: dict[str, Any]) -> dict[str, Any]:
    """Extract the worker's error envelope from a FAILED job record."""
    raw = record["error"]
    parsed: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else raw
    return parsed


def _decode_image(output: dict[str, Any]) -> tuple[int, int]:
    from PIL import Image

    raw = base64.b64decode(output["image_base64"])
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        return image.size


def test_prompt_returns_decodable_image() -> None:
    """The graded demonstration: text in, real image out."""
    record = _run_and_wait({"prompt": "a red fox in falling snow", "seed": 7})

    assert record["status"] == "COMPLETED"
    output = record["output"]
    width, height = _decode_image(output)
    assert (width, height) == (output["width"], output["height"])
    assert output["seed"] == 7
    assert output["model_version"].startswith("black-forest-labs/FLUX.1-dev@")


def test_runsync_returns_image() -> None:
    """The one-command demo path, against a warm worker."""
    output = _endpoint().run_sync(
        {"prompt": "a lighthouse at dusk", "num_inference_steps": 8},
        timeout=RUN_TIMEOUT_S,
    )

    assert "error" not in output
    _decode_image(output)


def test_same_seed_is_reproducible() -> None:
    """Same seed, same parameters, same reported configuration.

    Pixels are deliberately not compared: two invocations can land on
    different physical GPUs, and bf16 kernel selection is not bit-reproducible
    across hosts. The echoed seed and effective dimensions are the contract.
    """
    payload = {"prompt": "a clockwork owl", "seed": 1234, "num_inference_steps": 8}
    first = _run_and_wait(payload)["output"]
    second = _run_and_wait(payload)["output"]

    assert first["seed"] == second["seed"] == 1234
    assert (first["width"], first["height"]) == (second["width"], second["height"])
    assert first["model_version"] == second["model_version"]


def test_progress_is_visible_mid_generation() -> None:
    """Polling during a job returns non-decreasing percent values."""
    job = _endpoint().run({"prompt": "a slow render", "num_inference_steps": 50})
    percents: list[int] = []
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while time.monotonic() < deadline:
        status = job.status()
        if status in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            break
        if status == "IN_PROGRESS":
            output = job._fetch_job().get("output")  # noqa: SLF001 - SDK lacks a public partial-output accessor
            if isinstance(output, dict) and "percent" in output:
                percents.append(int(output["percent"]))
        time.sleep(POLL_INTERVAL_S)

    assert job.output() is not None
    assert percents == sorted(percents)
    assert len(set(percents)) >= 2


def test_blocked_prompt_returns_prompt_blocked() -> None:
    """The guardrail path through the real stack, ending in a clean envelope."""
    blocklist = json.loads(contract_path("blocklist.json").read_text())
    term = blocklist["categories"]["graphic_violence"][0]

    record = _run_and_wait({"prompt": f"a photo of {term}"})

    assert record["status"] == "FAILED"
    envelope = _error_envelope(record)
    assert envelope["code"] == "PROMPT_BLOCKED"
    assert envelope["suggestion"]


def test_dimensions_snap_and_are_reported() -> None:
    """1000px requests render at 992 — the effective values are the truth."""
    record = _run_and_wait(
        {
            "prompt": "a tiled courtyard",
            "width": 1000,
            "height": 1000,
            "num_inference_steps": 8,
        }
    )

    assert record["status"] == "COMPLETED"
    output = record["output"]
    assert output["width"] == 992
    assert output["height"] == 992
    assert _decode_image(output) == (992, 992)


def test_invalid_input_costs_no_gpu_time() -> None:
    """A bad request fails fast with a structured envelope, not a stack trace."""
    started = time.monotonic()
    record = _run_and_wait({"prompt": "", "width": 512})
    elapsed = time.monotonic() - started

    assert record["status"] == "FAILED"
    assert _error_envelope(record)["code"] == "INVALID_PROMPT"
    assert elapsed < 60
