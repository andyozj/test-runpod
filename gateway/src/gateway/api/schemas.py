"""Wire schemas. Validation happens here, at the boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gateway.core.models import GenerationParams, Job

MIN_DIMENSION = 256
MAX_DIMENSION = 1536
MAX_PROMPT_CHARS = 2000


class GenerationRequest(BaseModel):
    """Incoming generation parameters. Only `prompt` is required.

    Mirrors `contracts/generation-request.schema.json`.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "prompt": "a red fox in falling snow, cinematic lighting",
                "width": 1024,
                "height": 1024,
                "num_inference_steps": 28,
            }
        },
    )

    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    width: int = Field(default=1024, ge=MIN_DIMENSION, le=MAX_DIMENSION)
    height: int = Field(default=1024, ge=MIN_DIMENSION, le=MAX_DIMENSION)
    num_inference_steps: int = Field(default=28, ge=1, le=50)
    guidance_scale: float = Field(default=3.5, ge=0, le=20)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    output_format: Literal["png", "jpeg"] = "png"

    @field_validator("prompt")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            msg = "prompt must not be blank"
            raise ValueError(msg)
        return value

    def to_params(self) -> GenerationParams:
        """Convert to the domain type.

        Returns:
            The equivalent `GenerationParams`.
        """
        return GenerationParams(**self.model_dump())


class JobCreated(BaseModel):
    """Response to a successful submission."""

    job_id: UUID
    status: str
    created_at: datetime


class ErrorBody(BaseModel):
    """The error envelope every failure shares."""

    code: str
    message: str
    suggestion: str | None = None
    correlation_id: str | None = None


class ErrorResponse(BaseModel):
    """Wrapper so errors are distinguishable from results at a glance."""

    error: ErrorBody


class JobView(BaseModel):
    """Full job state.

    One shape with three populated states — running, completed, failed — so a
    client parses one response type regardless of outcome.
    """

    job_id: UUID
    status: str
    progress: dict[str, int] | None = None
    result: dict[str, Any] | None = None
    error: ErrorBody | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @classmethod
    def of(cls, job: Job) -> JobView:
        """Render a domain job for the wire.

        Args:
            job: The job to render.

        Returns:
            The wire representation.
        """
        return cls(
            job_id=job.id,
            status=job.status.value,
            progress=(
                {
                    "step": job.progress.step,
                    "total": job.progress.total,
                    "percent": job.progress.percent,
                }
                if job.progress
                else None
            ),
            result=(
                {
                    "image_base64": job.result.image_base64,
                    "format": job.result.format,
                    "seed": job.result.seed,
                    "width": job.result.width,
                    "height": job.result.height,
                    "model_version": job.result.model_version,
                    "inference_seconds": job.result.inference_seconds,
                }
                if job.result
                else None
            ),
            error=(
                ErrorBody(
                    code=job.error_code.value,
                    message=job.error_message or "",
                    correlation_id=job.context.correlation_id,
                )
                if job.error_code
                else None
            ),
            created_at=job.created_at,
            updated_at=job.updated_at,
            completed_at=job.completed_at,
        )
