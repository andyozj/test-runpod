"""Pure generation logic against a fake pipeline. No GPU."""

from __future__ import annotations

import base64
import io

from PIL import Image

from tests.conftest import FakePipeline
from worker.inference import generate
from worker.schemas import GenerationRequest
from worker.settings import Settings


def test_jpeg_output_format_is_encoded_as_jpeg(
    pipeline: FakePipeline, settings: Settings
) -> None:
    request = GenerationRequest(prompt="x", output_format="jpeg")

    result = generate(request, pipeline, settings)

    assert result.format == "jpeg"
    decoded = base64.b64decode(result.image_base64)
    image = Image.open(io.BytesIO(decoded))
    assert image.format == "JPEG"


def test_png_output_format_is_encoded_as_png(
    pipeline: FakePipeline, settings: Settings
) -> None:
    request = GenerationRequest(prompt="x", output_format="png")

    result = generate(request, pipeline, settings)

    assert result.format == "png"
    decoded = base64.b64decode(result.image_base64)
    image = Image.open(io.BytesIO(decoded))
    assert image.format == "PNG"
