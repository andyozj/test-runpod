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


def _load_pipeline() -> ImagePipeline:  # pragma: no cover - requires a GPU
    """Load FLUX.1-dev from the configured weights path onto the GPU.

    Imports `torch` and `diffusers` inside the function so that merely
    importing this module — which is what the test suite does — never requires
    them.

    Returns:
        The pipeline, resident on CUDA.

    Raises:
        WeightsNotFoundError: No usable weights were found. See `weights.resolve`.
    """
    import torch
    from diffusers import FluxPipeline

    from worker import weights

    settings = get_settings()
    path = weights.resolve(settings)
    # Report the revision the snapshot actually holds; the env value is only
    # a fallback for layouts that carry no evidence.
    discovered = weights.discovered_revision(path)
    if discovered is not None:
        settings.model_revision = discovered

    started = time.perf_counter()
    pipe = FluxPipeline.from_pretrained(
        path,
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
