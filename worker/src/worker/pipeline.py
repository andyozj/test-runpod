"""Lazy access to the diffusion pipeline.

The pipeline must load once per worker rather than once per job, and
`handler.py` must remain importable without a GPU. A module-level constant
satisfies the first and breaks the second; this accessor satisfies both.
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

import structlog

from worker.settings import get_settings

logger = structlog.get_logger()

_pipeline: ImagePipeline | None = None


@runtime_checkable
class ImagePipeline(Protocol):
    """The slice of `diffusers.FluxPipeline` this worker actually uses.

    Declared locally rather than importing `FluxPipeline` directly: `diffusers`
    is not usefully typed, so under `mypy strict` everything from it arrives as
    `Any` and trips `warn_return_any` at the first boundary crossing. The
    protocol is what makes strict mode viable and what tests fake.
    """

    def __call__(self, **kwargs: Any) -> Any:
        """Run inference and return an object exposing `.images`.

        Args:
            **kwargs: Pipeline arguments such as prompt, width and height.

        Returns:
            An object whose `images` attribute is a list of PIL images.
        """
        ...


def get_pipeline() -> ImagePipeline:
    """Return the process-wide pipeline, loading it on first use.

    Returns:
        The loaded pipeline.
    """
    global _pipeline
    if _pipeline is None:
        _pipeline = _load_pipeline()
    return _pipeline


def set_pipeline(pipeline: ImagePipeline | None) -> None:
    """Replace the process-wide pipeline. Tests only.

    Args:
        pipeline: The replacement, or None to reset.
    """
    global _pipeline
    _pipeline = pipeline


def _load_pipeline() -> ImagePipeline:  # pragma: no cover - requires a GPU
    """Load FLUX.1-dev from the configured weights path onto the GPU.

    Imports `torch` and `diffusers` inside the function so that merely
    importing this module — which is what the test suite does — never requires
    them.

    Returns:
        The pipeline, resident on CUDA.

    Raises:
        RuntimeError: If the configured weights path does not exist. Failing
            fast here prevents a misconfigured volume mount from silently
            falling back to downloading 33GB on every cold start, which
            presents as "slow" rather than "broken".
    """
    import torch
    from diffusers import FluxPipeline

    settings = get_settings()
    if not settings.weights_path.exists():
        msg = (
            f"Weights not found at {settings.weights_path}. "
            "Check WEIGHTS_PATH and, for the volume variant, that the network "
            "volume is mounted and in the same datacenter as the endpoint."
        )
        raise RuntimeError(msg)

    started = time.perf_counter()
    pipe = FluxPipeline.from_pretrained(
        settings.weights_path,
        torch_dtype=torch.bfloat16,
        local_files_only=True,
    )
    pipe.to("cuda")
    pipe.set_progress_bar_config(disable=True)
    logger.info(
        "pipeline_loaded",
        duration_s=round(time.perf_counter() - started, 2),
        model_version=settings.model_version,
    )
    return pipe  # type: ignore[no-any-return]
