"""Request and result schemas for the worker's job contract."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

LATENT_MULTIPLE = 16
MIN_DIMENSION = 256
MAX_DIMENSION = 1536
MAX_PROMPT_CHARS = 2000


def snap_to_multiple(value: int, multiple: int = LATENT_MULTIPLE) -> int:
    """Round a dimension down to the nearest multiple.

    FLUX latents are 16x downsampled, so dimensions that are not multiples of
    16 produce distorted output.

    Args:
        value: The requested dimension in pixels.
        multiple: The required factor.

    Returns:
        The largest multiple of `multiple` not exceeding `value`.

    Example:
        >>> snap_to_multiple(1000)
        992
        >>> snap_to_multiple(1024)
        1024
    """
    return value - (value % multiple)


class GenerationRequest(BaseModel):
    """Validated parameters for one image generation.

    Mirrors `contracts/generation-request.schema.json`. FLUX.1-dev is
    guidance-distilled and takes no negative prompt, so the field is absent
    rather than accepted and ignored.

    Attributes:
        prompt: The text prompt. Capped at 2000 characters.
        width: Output width, snapped down to a multiple of 16.
        height: Output height, snapped down to a multiple of 16.
        num_inference_steps: Denoising steps.
        guidance_scale: Embedded guidance strength.
        seed: Populated by the caller when absent so it is always recorded.
        output_format: Encoding of the returned image.
        correlation_id: Bound into the log context to trace across tiers.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    width: int = Field(default=1024, ge=MIN_DIMENSION, le=MAX_DIMENSION)
    height: int = Field(default=1024, ge=MIN_DIMENSION, le=MAX_DIMENSION)
    num_inference_steps: int = Field(default=28, ge=1, le=50)
    guidance_scale: float = Field(default=3.5, ge=0, le=20)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    output_format: Literal["png", "jpeg"] = "png"
    correlation_id: str | None = None

    @field_validator("prompt")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            msg = "prompt must not be blank"
            raise ValueError(msg)
        return value

    @property
    def effective_width(self) -> int:
        """Width actually rendered, after snapping.

        Returns:
            The requested width rounded down to a multiple of 16.
        """
        return snap_to_multiple(self.width)

    @property
    def effective_height(self) -> int:
        """Height actually rendered, after snapping.

        Returns:
            The requested height rounded down to a multiple of 16.
        """
        return snap_to_multiple(self.height)


class GenerationResult(BaseModel):
    """What one successful generation produced.

    Exactly one of `image_base64` and `storage_key` is populated. Base64 is the
    default because RunPod's `GET /status/{job_id}` returns the handler output
    verbatim, so a key would be unresolvable by a direct caller.

    Attributes:
        image_base64: The encoded image, when storage is disabled.
        storage_key: Object key, when storage is enabled.
        format: Image encoding.
        seed: The seed actually used, including when randomly chosen.
        width: Rendered width.
        height: Rendered height.
        num_inference_steps: Steps actually run.
        guidance_scale: Guidance actually applied.
        model_version: Repository and pinned revision that produced this.
        timings: Wall-clock durations by stage, in seconds.
    """

    image_base64: str | None = None
    storage_key: str | None = None
    format: str
    seed: int
    width: int
    height: int
    num_inference_steps: int
    guidance_scale: float
    model_version: str
    timings: dict[str, float]

    def as_output(self) -> dict[str, Any]:
        """Return the result as the handler's job output.

        Returns:
            A JSON-serialisable dict.
        """
        return self.model_dump()
