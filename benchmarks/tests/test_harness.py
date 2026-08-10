# ruff: noqa: D103, S101, PLR2004
"""Unit tests for the public-endpoint adapters in benchmarks/harness.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import (  # noqa: E402 - path setup must precede this import
    FakePublicApi,
    public_cost_usd,
    public_flux_payload,
    worker_cost_usd,
)


def test_public_payload_renames_worker_fields() -> None:
    canonical = {
        "prompt": "a red fox in falling snow, cinematic lighting",
        "seed": 42,
        "width": 1024,
        "height": 1024,
        "num_inference_steps": 28,
        "guidance_scale": 3.5,
        "output_format": "jpeg",
    }

    assert public_flux_payload(canonical) == {
        "prompt": "a red fox in falling snow, cinematic lighting",
        "seed": 42,
        "width": 1024,
        "height": 1024,
        "num_inference_steps": 28,
        "guidance": 3.5,
        "image_format": "jpeg",
    }


def test_public_payload_drops_fields_the_public_schema_lacks() -> None:
    assert public_flux_payload({"prompt": "x", "lora_scale": 0.8}) == {"prompt": "x"}


def test_public_cost_prefers_the_reported_cost() -> None:
    assert public_cost_usd({"cost": 0.031}, 1024, 1024, 0.02) == 0.031


def test_public_cost_falls_back_to_dims_times_rate() -> None:
    # 1024x1024 = 1.048576 MP at $0.02/MP: the docs' own worked example.
    cost = public_cost_usd({"image_url": "https://image.runpod.ai/x"}, 1024, 1024, 0.02)

    assert abs(cost - 0.02097152) < 1e-9


def test_worker_cost_is_rate_times_execution_seconds() -> None:
    # 21.8s at $1.75/hr, the measured 28-step p50.
    assert abs(worker_cost_usd(21800, 1.75) - 0.01059722) < 1e-6


def test_fake_public_api_matches_the_live_output_shape() -> None:
    # Measured 2026-08-10: the endpoint returns the URL under `result`, not
    # the `image_url` its docs describe. The fixture prices at the published
    # $0.02/MP; live invoices came in flat at $0.0120/image, so this cost is
    # the ceiling a dry run should estimate against, not the billed figure.
    api = FakePublicApi()

    job = api.wait(api.submit({"prompt": "x", "width": 1024, "height": 1024}))

    assert job["status"] == "COMPLETED"
    assert job["output"]["result"].startswith("https://image.runpod.ai/")
    assert "image_url" not in job["output"]
    assert abs(job["output"]["cost"] - 0.02097152) < 1e-9
    assert "image_base64" not in job["output"]
