"""Pure generation logic. Takes a pipeline, returns a result. No globals."""

from __future__ import annotations

import base64
import io
import secrets
import time
from collections.abc import Callable
from typing import Any

import structlog
from PIL import Image

from worker.errors import ErrorCode, InferenceError, is_out_of_memory
from worker.pipeline import ImagePipeline
from worker.schemas import GenerationRequest, GenerationResult
from worker.settings import Settings

logger = structlog.get_logger()

MAX_SEED = 2**31 - 1
T5_MAX_SEQUENCE_LENGTH = 512
JPEG_QUALITY = 95

ProgressCallback = Callable[[int, int], None]


def encode_image(image: Image.Image, image_format: str) -> bytes:
    """Encode a PIL image to bytes.

    Args:
        image: The generated image.
        image_format: Either `png` or `jpeg`.

    Returns:
        The encoded bytes.
    """
    buffer = io.BytesIO()
    if image_format == "jpeg":
        image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def generate(
    request: GenerationRequest,
    pipeline: ImagePipeline,
    settings: Settings,
    on_progress: ProgressCallback | None = None,
) -> GenerationResult:
    """Run FLUX.1-dev inference for a single prompt.

    Dimensions are snapped down to the nearest multiple of 16 before inference,
    since the FLUX latent space is 16x downsampled, and the snapped values are
    returned so callers see what was actually rendered. A seed is generated
    when absent and always echoed, so every output is reproducible.

    Args:
        request: Validated generation parameters.
        pipeline: An initialised pipeline, already resident on the GPU.
        settings: Runtime configuration, used for `model_version`.
        on_progress: Called after each denoising step with the completed step
            number and the total. Must stay trivial — it runs on the GPU thread
            between steps, so the cost is paid once per step.

    Returns:
        The rendered image with the effective seed, dimensions and timings.

    Raises:
        InferenceError: The pipeline failed. VRAM exhaustion is reported with
            the `OOM` code so the caller can retire the worker; every other
            failure is `INFERENCE_FAILED` with the detail logged rather than
            returned.
    """
    seed = request.seed if request.seed is not None else secrets.randbelow(MAX_SEED)
    width = request.effective_width
    height = request.effective_height
    total_steps = request.num_inference_steps

    kwargs: dict[str, Any] = {
        "prompt": request.prompt,
        "width": width,
        "height": height,
        "num_inference_steps": total_steps,
        "guidance_scale": request.guidance_scale,
        "max_sequence_length": T5_MAX_SEQUENCE_LENGTH,
        "generator": _generator(seed),
    }
    if on_progress is not None:
        kwargs["callback_on_step_end"] = _step_callback(on_progress, total_steps)

    started = time.perf_counter()
    try:
        image = pipeline(**kwargs).images[0]
    except Exception as exc:
        raise _classify(exc) from exc
    inference_s = time.perf_counter() - started

    encode_started = time.perf_counter()
    payload = encode_image(image, request.output_format)
    encode_s = time.perf_counter() - encode_started

    return GenerationResult(
        image_base64=base64.b64encode(payload).decode("ascii"),
        format=request.output_format,
        seed=seed,
        width=width,
        height=height,
        num_inference_steps=total_steps,
        guidance_scale=request.guidance_scale,
        model_version=settings.model_version,
        timings={
            "inference_s": round(inference_s, 3),
            "encode_s": round(encode_s, 3),
        },
    )


def _generator(
    seed: int,
) -> Any | None:  # torch.Generator; torch is import-guarded below
    """Return a seeded CUDA generator, or None when torch is unavailable.

    Args:
        seed: The seed to use.

    Returns:
        A `torch.Generator` on CUDA, or None in a torch-free environment such
        as the test suite.
    """
    try:
        import torch
    except ImportError:  # pragma: no cover - torch is present in the image
        return None
    return torch.Generator("cuda").manual_seed(seed)


def _step_callback(
    on_progress: ProgressCallback, total: int
) -> Callable[..., dict[str, Any]]:
    """Adapt our progress callback to the diffusers callback signature.

    Args:
        on_progress: The callback to invoke with (completed_step, total).
        total: Total number of steps.

    Returns:
        A callable matching `callback_on_step_end`.
    """

    def _callback(
        _pipe: Any, step: int, _timestep: int, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        on_progress(step + 1, total)
        return kwargs

    return _callback


def _classify(exc: Exception) -> InferenceError:
    """Map a pipeline exception to a caller-visible error.

    Args:
        exc: The exception raised by the pipeline.

    Returns:
        The error to raise in its place.
    """
    if is_out_of_memory(exc):
        logger.error("oom_detected", error=str(exc))
        return InferenceError(
            ErrorCode.OOM,
            "GPU memory exhausted while generating.",
            "Retry at a lower resolution or fewer steps.",
        )
    logger.error("inference_failed", error=str(exc), error_type=type(exc).__name__)
    return InferenceError(
        ErrorCode.INFERENCE_FAILED,
        "Image generation failed.",
        "Retry; if it persists, reduce resolution or steps.",
    )
