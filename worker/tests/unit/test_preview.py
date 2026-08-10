"""Latent preview: unpack math, projection, size bounds, fail-open. No GPU."""

from __future__ import annotations

import base64
import io
from typing import Any

import numpy as np
import pytest
from PIL import Image

from tests.conftest import FakePipeline
from worker import handler as handler_module
from worker import preview
from worker.inference import generate
from worker.schemas import GenerationRequest
from worker.settings import Settings

RNG = np.random.default_rng(42)


def pack_latents(spatial: np.ndarray) -> np.ndarray:
    """Numpy port of FluxPipeline._pack_latents (diffusers 0.39.0)."""
    batch, channels, height, width = spatial.shape
    packed = spatial.reshape(batch, channels, height // 2, 2, width // 2, 2)
    packed = packed.transpose(0, 2, 4, 1, 3, 5)
    return packed.reshape(batch, (height // 2) * (width // 2), channels * 4)


def packed_for(width: int, height: int, rng: np.random.Generator = RNG) -> np.ndarray:
    spatial = rng.standard_normal(
        (1, preview.LATENT_CHANNELS, height // 8, width // 8), dtype=np.float32
    )
    return pack_latents(spatial)


# --- unpack math ---


def test_unpack_inverts_the_diffusers_pack_transform() -> None:
    spatial = RNG.standard_normal((1, 16, 8, 12), dtype=np.float32)

    roundtripped = preview.unpack_latents(pack_latents(spatial), 64, 96)

    np.testing.assert_array_equal(roundtripped, spatial)


def test_unpack_shapes_at_1024() -> None:
    packed = packed_for(1024, 1024)

    assert packed.shape == (1, 4096, 64)
    assert preview.unpack_latents(packed, 1024, 1024).shape == (1, 16, 128, 128)


def test_unpack_rejects_mismatched_dimensions() -> None:
    with pytest.raises(ValueError):
        preview.unpack_latents(packed_for(512, 512), 1024, 1024)


# --- projection and encoding ---


def test_render_preview_is_deterministic() -> None:
    packed = packed_for(256, 256)

    assert preview.render_preview(packed, 256, 256) == preview.render_preview(
        packed, 256, 256
    )


def test_projection_maps_zero_latents_to_the_bias_grey() -> None:
    image = preview.latents_to_rgb(np.zeros((1, 16, 8, 8), dtype=np.float32))

    expected = tuple(round((b + 1.0) / 2.0 * 255) for b in (-0.0329, -0.0718, -0.0851))
    assert image.getpixel((0, 0)) == pytest.approx(expected, abs=1)


@pytest.mark.parametrize("side", [1024, 1536])
def test_preview_frame_is_bounded(side: int) -> None:
    frame = base64.b64decode(preview.render_preview(packed_for(side, side), side, side))

    # Random-noise latents are the JPEG worst case; real mid-denoise frames
    # compress smaller.
    assert len(frame) <= 15 * 1024
    image = Image.open(io.BytesIO(frame))
    assert image.format == "JPEG"
    assert max(image.size) <= preview.MAX_PREVIEW_SIDE


# --- reporter wiring ---


def _reported(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    import runpod

    payloads: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runpod.serverless,
        "progress_update",
        lambda _job, progress: payloads.append(progress),
    )
    return payloads


def test_reported_strides_carry_a_preview(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = _reported(monkeypatch)
    request = GenerationRequest(prompt="x", width=256, height=256)
    pipeline = FakePipeline(latents=packed_for(256, 256))

    generate(
        request,
        pipeline,
        settings,
        on_progress=handler_module._progress_reporter(
            {"id": "job-1"}, request, settings
        ),
    )

    assert payloads
    for payload in payloads:
        assert payload["preview_format"] == "jpeg"
        frame = base64.b64decode(payload["preview_b64"])
        assert Image.open(io.BytesIO(frame)).format == "JPEG"


def test_preview_failure_degrades_to_plain_progress(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = _reported(monkeypatch)
    request = GenerationRequest(prompt="x", num_inference_steps=4)
    reporter = handler_module._progress_reporter({"id": "job-1"}, request, settings)
    assert reporter is not None

    reporter(4, 4, np.zeros((3, 3), dtype=np.float32))  # unpackable shape

    assert payloads == [{"step": 4, "total": 4, "percent": 100}]


def test_preview_disabled_sends_plain_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = _reported(monkeypatch)
    settings = Settings(preview_enabled=False)
    request = GenerationRequest(prompt="x", width=256, height=256)
    reporter = handler_module._progress_reporter({"id": "job-1"}, request, settings)
    assert reporter is not None

    reporter(28, 28, packed_for(256, 256))

    assert payloads == [{"step": 28, "total": 28, "percent": 100}]


def test_absent_latents_send_plain_progress(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = _reported(monkeypatch)
    request = GenerationRequest(prompt="x")
    reporter = handler_module._progress_reporter({"id": "job-1"}, request, settings)
    assert reporter is not None

    reporter(28, 28, None)

    assert payloads == [{"step": 28, "total": 28, "percent": 100}]
