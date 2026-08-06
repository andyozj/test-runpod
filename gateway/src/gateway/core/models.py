"""Domain types. No framework types, no database types, no RunPod vocabulary."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from gateway.contracts import contract_path


class JobStatus(StrEnum):
    """Lifecycle of one generation request.

    `BLOCKED` is distinct from `FAILED` because a guardrail rejection is a
    decision, not an error, and conflating them makes the block rate
    unmeasurable.
    """

    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"

    @property
    def terminal(self) -> bool:
        """Whether no further transition is possible.

        Returns:
            True for states that are written once and never changed.

        Example:
            >>> JobStatus.COMPLETED.terminal
            True
            >>> JobStatus.QUEUED.terminal
            False
        """
        return self in _TERMINAL


_TERMINAL = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.TIMED_OUT,
        JobStatus.CANCELLED,
        JobStatus.BLOCKED,
    }
)


class ErrorCode(StrEnum):
    """Stable machine-readable codes, mirrored in `contracts/error-codes.json`."""

    INVALID_PROMPT = "INVALID_PROMPT"
    INVALID_DIMENSIONS = "INVALID_DIMENSIONS"
    INVALID_STEPS = "INVALID_STEPS"
    PROMPT_BLOCKED = "PROMPT_BLOCKED"
    IMAGE_BLOCKED = "IMAGE_BLOCKED"
    OOM = "OOM"
    INFERENCE_FAILED = "INFERENCE_FAILED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_TIMEOUT = "JOB_TIMEOUT"
    JOB_CANCELLED = "JOB_CANCELLED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    QUEUE_SATURATED = "QUEUE_SATURATED"


def contract_codes() -> set[str]:
    """Return the error-code set declared in the shared contract.

    Returns:
        Every code name in `contracts/error-codes.json`.
    """
    data = json.loads(contract_path("error-codes.json").read_text())
    codes: list[str] = data["codes"]
    return set(codes)


@dataclass(frozen=True)
class RequestContext:
    """Who is asking, and under what trace.

    Attributes:
        api_key_id: Resolved caller identity, attributed on the job and logs.
        correlation_id: Traces one request across both tiers.
        idempotency_key: Client-supplied key enabling safe retries.
    """

    api_key_id: str
    correlation_id: str
    idempotency_key: str | None = None


@dataclass(frozen=True)
class GenerationParams:
    """Validated generation parameters, mirroring the shared request contract.

    Attributes:
        prompt: The text prompt.
        width: Requested width in pixels.
        height: Requested height in pixels.
        num_inference_steps: Denoising steps.
        guidance_scale: Embedded guidance strength.
        seed: Fixed seed, or None to let the worker choose.
        output_format: Image encoding.
    """

    prompt: str
    width: int = 1024
    height: int = 1024
    num_inference_steps: int = 28
    guidance_scale: float = 3.5
    seed: int | None = None
    output_format: str = "png"

    def as_worker_input(self, correlation_id: str) -> dict[str, Any]:
        """Render the parameters as the worker's job input.

        Args:
            correlation_id: Threaded through so the worker binds it to its logs.

        Returns:
            A JSON-serialisable dict matching the worker's contract.
        """
        payload: dict[str, Any] = {
            "prompt": self.prompt,
            "width": self.width,
            "height": self.height,
            "num_inference_steps": self.num_inference_steps,
            "guidance_scale": self.guidance_scale,
            "output_format": self.output_format,
            "correlation_id": correlation_id,
        }
        if self.seed is not None:
            payload["seed"] = self.seed
        return payload

    def fingerprint(self) -> str:
        """Return a stable hash of the parameters.

        Used to detect an idempotency key replayed with a different body, which
        would otherwise hand the caller an image of something they did not ask
        for with nothing to indicate it.

        Returns:
            A hex digest.

        Example:
            >>> a = GenerationParams(prompt="fox")
            >>> b = GenerationParams(prompt="fox")
            >>> a.fingerprint() == b.fingerprint()
            True
            >>> a.fingerprint() == GenerationParams(prompt="cat").fingerprint()
            False
        """
        import hashlib

        canonical = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class JobResult:
    """What the worker produced.

    Attributes:
        image_base64: The encoded image.
        format: Image encoding.
        seed: The seed actually used.
        width: Rendered width.
        height: Rendered height.
        model_version: Repository and revision that produced this.
        inference_seconds: Wall-clock generation time.
    """

    image_base64: str | None
    format: str
    seed: int
    width: int
    height: int
    model_version: str
    inference_seconds: float


@dataclass(frozen=True)
class Progress:
    """Latest per-step progress reported by the worker.

    Attributes:
        step: Completed steps.
        total: Total steps.
        percent: Convenience percentage.
    """

    step: int
    total: int
    percent: int


@dataclass(frozen=True)
class Job:
    """A generation request and everything known about it so far."""

    id: UUID
    status: JobStatus
    params: GenerationParams
    context: RequestContext
    created_at: datetime
    updated_at: datetime
    runpod_job_id: str | None = None
    result: JobResult | None = None
    progress: Progress | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    completed_at: datetime | None = None
    request_hash: str = field(default="")

    def advanced(self, **changes: Any) -> Job:
        """Return a copy with the given changes applied.

        Immutability means a transition produces a new `Job` rather than
        mutating one another caller still holds.

        Args:
            **changes: Fields to replace.

        Returns:
            The updated job.
        """
        return replace(self, **changes)
