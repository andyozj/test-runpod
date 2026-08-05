"""Domain error codes and exceptions for the worker."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

_CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "error-codes.json"


class ErrorCode(StrEnum):
    """Stable machine-readable error codes shared across both tiers.

    The set is mirrored in `contracts/error-codes.json`, which both packages
    assert against so the two tiers cannot drift apart silently.
    """

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
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    QUEUE_SATURATED = "QUEUE_SATURATED"


def contract_codes() -> set[str]:
    """Return the error-code set declared in the shared contract file.

    Returns:
        Every code name listed in `contracts/error-codes.json`.
    """
    data = json.loads(_CONTRACT.read_text())
    codes: list[str] = data["codes"]
    return set(codes)


class WorkerError(Exception):
    """Base for errors that map to a caller-visible error code.

    Args:
        code: The stable code returned to the caller.
        message: Human-readable description, safe to expose.
        suggestion: The next valid action, where one exists.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        suggestion: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion

    def envelope(self) -> dict[str, Any]:
        """Return the error as the wire-format envelope.

        Returns:
            A dict with a single `error` key carrying code, message and
            suggestion.

        Example:
            >>> err = WorkerError(ErrorCode.OOM, "VRAM exhausted.")
            >>> err.envelope()["error"]["code"]
            'OOM'
        """
        body: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.suggestion is not None:
            body["suggestion"] = self.suggestion
        return {"error": body}


class InvalidInputError(WorkerError):
    """Raised when a job payload fails validation."""


class GuardrailBlockedError(WorkerError):
    """Raised when a guardrail rejects the prompt or the generated image."""


class InferenceError(WorkerError):
    """Raised when the diffusion pipeline fails for an unclassified reason."""


def is_out_of_memory(exc: BaseException) -> bool:
    """Report whether an exception is a CUDA out-of-memory error.

    Detection is by class name rather than an `isinstance` check against
    `torch.cuda.OutOfMemoryError`, so this module — and therefore the handler —
    stays importable without torch installed.

    Args:
        exc: The exception to classify.

    Returns:
        True if the exception represents VRAM exhaustion.

    Example:
        >>> class OutOfMemoryError(Exception): pass
        >>> is_out_of_memory(OutOfMemoryError())
        True
        >>> is_out_of_memory(ValueError())
        False
    """
    if type(exc).__name__ == "OutOfMemoryError":
        return True
    return "CUDA out of memory" in str(exc)
