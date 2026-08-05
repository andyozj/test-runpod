"""RunPod serverless entrypoint. Parse, guard, delegate, serialise.

Holds no inference logic, which is what keeps it testable without a GPU.
"""

from __future__ import annotations

import hashlib
from typing import Any

import structlog
from pydantic import ValidationError

from worker.errors import ErrorCode, GuardrailBlockedError, WorkerError
from worker.guardrails import (
    BlocklistPromptGuardrail,
    ImageGuardrail,
    NoopImageGuardrail,
    PromptGuardrail,
)
from worker.inference import ProgressCallback, generate
from worker.pipeline import ImagePipeline, get_pipeline
from worker.schemas import GenerationRequest, GenerationResult
from worker.settings import Settings, get_settings

logger = structlog.get_logger()

PROMPT_PREVIEW_CHARS = 80

_prompt_guardrail: PromptGuardrail = BlocklistPromptGuardrail()
_image_guardrail: ImageGuardrail = NoopImageGuardrail()


def set_guardrails(
    prompt: PromptGuardrail | None = None,
    image: ImageGuardrail | None = None,
) -> None:
    """Replace the process-wide guardrails. Tests and composition root only.

    Args:
        prompt: Replacement prompt guardrail, or None to leave unchanged.
        image: Replacement image guardrail, or None to leave unchanged.
    """
    global _prompt_guardrail, _image_guardrail
    if prompt is not None:
        _prompt_guardrail = prompt
    if image is not None:
        _image_guardrail = image


def handler(job: dict[str, Any]) -> dict[str, Any]:
    """Handle one RunPod job.

    Args:
        job: The RunPod job envelope, carrying an `input` dict.

    Returns:
        The generation result as a plain dict, or an error envelope. VRAM
        exhaustion additionally sets `refresh_worker`, because fragmentation
        outlives `empty_cache()` and the worker cannot be trusted with the next
        job.
    """
    job_input = job.get("input") or {}
    try:
        request = _parse(job_input)
    except WorkerError as exc:
        return exc.envelope()

    log = logger.bind(
        correlation_id=request.correlation_id,
        prompt_preview=request.prompt[:PROMPT_PREVIEW_CHARS],
        prompt_length=len(request.prompt),
        prompt_sha=hashlib.sha256(request.prompt.encode()).hexdigest()[:12],
    )

    try:
        _guard_prompt(request)
        result = _run(job, request, log)
        _guard_image(result.image_base64)
    except WorkerError as exc:
        return _error_output(exc, log)

    log.info(
        "generation_completed",
        seed=result.seed,
        width=result.width,
        height=result.height,
        steps=result.num_inference_steps,
        **result.timings,
    )
    return result.as_output()


def _parse(job_input: dict[str, Any]) -> GenerationRequest:
    """Validate the job input into a request.

    Args:
        job_input: The raw `input` dict from the job envelope.

    Returns:
        The validated request.

    Raises:
        WorkerError: The payload failed validation.
    """
    try:
        return GenerationRequest.model_validate(job_input)
    except ValidationError as exc:
        raise WorkerError(
            ErrorCode.INVALID_PROMPT if _is_prompt_error(exc) else _field_code(exc),
            _first_message(exc),
            "Correct the highlighted field and resubmit.",
        ) from exc


def _is_prompt_error(exc: ValidationError) -> bool:
    return any("prompt" in error["loc"] for error in exc.errors())


def _field_code(exc: ValidationError) -> ErrorCode:
    fields = {str(loc) for error in exc.errors() for loc in error["loc"]}
    if fields & {"width", "height"}:
        return ErrorCode.INVALID_DIMENSIONS
    if "num_inference_steps" in fields:
        return ErrorCode.INVALID_STEPS
    return ErrorCode.INVALID_PROMPT


def _first_message(exc: ValidationError) -> str:
    error = exc.errors()[0]
    field = ".".join(str(loc) for loc in error["loc"]) or "input"
    return f"{field}: {error['msg']}"


def _guard_prompt(request: GenerationRequest) -> None:
    """Reject a prompt before any GPU time is spent.

    Args:
        request: The validated request.

    Raises:
        GuardrailBlockedError: The prompt was blocked.
    """
    verdict = _prompt_guardrail.check(request.prompt)
    if verdict.blocked:
        raise GuardrailBlockedError(
            ErrorCode.PROMPT_BLOCKED,
            verdict.reason or "Prompt rejected by content policy.",
            "Rephrase the prompt and resubmit.",
        )


def _guard_image(image_base64: str | None) -> None:
    """Reject a generated image before it is returned or uploaded.

    Runs before any upload: storing first and deleting after is a race the
    blocker loses, and the object may already have been referenced.

    Args:
        image_base64: The encoded image, if one was produced.

    Raises:
        GuardrailBlockedError: The image was blocked.
    """
    if image_base64 is None:
        return
    verdict = _image_guardrail.check(image_base64.encode("ascii"))
    if verdict.blocked:
        raise GuardrailBlockedError(
            ErrorCode.IMAGE_BLOCKED,
            verdict.reason or "Generated image rejected by content policy.",
            "Try a different prompt or seed.",
        )


def _run(
    job: dict[str, Any],
    request: GenerationRequest,
    log: Any,
    pipeline: ImagePipeline | None = None,
    settings: Settings | None = None,
) -> GenerationResult:
    """Generate the image, reporting progress to RunPod.

    Args:
        job: The RunPod job envelope, needed for `progress_update`.
        request: The validated request.
        log: Bound logger for this job.
        pipeline: Override for the pipeline. Tests only.
        settings: Override for settings. Tests only.

    Returns:
        The generation result.
    """
    log.info("generation_started", steps=request.num_inference_steps)
    return generate(
        request,
        pipeline if pipeline is not None else get_pipeline(),
        settings if settings is not None else get_settings(),
        on_progress=_progress_reporter(job),
    )


PROGRESS_STRIDE_PCT = 10


def should_report_progress(step: int, total: int, last_percent: int) -> bool:
    """Decide whether this step's progress is worth an upstream call.

    The SDK's `progress_update` spawns a thread, an event loop, and a TLS
    session per call, so per-step reporting costs 28 of each per image for
    granularity no poller can observe. Reporting every `PROGRESS_STRIDE_PCT`
    bounds the cost at ~10 calls per job regardless of step count.

    Args:
        step: The completed step, 1-based.
        total: Total steps.
        last_percent: The percent value most recently reported.

    Returns:
        True for the final step and whenever progress advanced a full stride.
    """
    percent = round(100 * step / total)
    return step == total or percent >= last_percent + PROGRESS_STRIDE_PCT


def _progress_reporter(job: dict[str, Any]) -> ProgressCallback | None:
    """Build a throttled progress callback writing into the RunPod job record.

    Progress is not logged: it is already visible on the job, and 28 log lines
    per image describe something nobody reads twice.

    Args:
        job: The RunPod job envelope.

    Returns:
        A callable taking the completed step and the total, or None when the
        RunPod SDK is unavailable.
    """
    if not job.get("id"):
        return None
    try:
        import runpod
    except ImportError:  # pragma: no cover - runpod is present in the image
        return None

    last = {"percent": -PROGRESS_STRIDE_PCT}

    def _report(step: int, total: int) -> None:
        if not should_report_progress(step, total, last["percent"]):
            return
        percent = round(100 * step / total)
        last["percent"] = percent
        runpod.serverless.progress_update(
            job, {"step": step, "total": total, "percent": percent}
        )

    return _report


def _error_output(exc: WorkerError, log: Any) -> dict[str, Any]:
    """Serialise an error, retiring the worker when VRAM is exhausted.

    Args:
        exc: The error to report.
        log: Bound logger for this job.

    Returns:
        The error envelope, with `refresh_worker` set for OOM.
    """
    log.warning("job_failed", code=exc.code.value)
    output = exc.envelope()
    if exc.code is ErrorCode.OOM:
        _empty_cache()
        output["refresh_worker"] = True
    return output


def _empty_cache() -> None:  # pragma: no cover - requires torch
    """Release cached VRAM before the worker is retired."""
    try:
        import torch

        torch.cuda.empty_cache()
    except ImportError:
        return


def main() -> None:  # pragma: no cover - process entrypoint
    """Warm the pipeline, then serve.

    Loading here rather than at import means production workers pay the cost
    during container start instead of during the first billed job, while
    importing this module never touches a GPU.
    """
    import runpod

    get_pipeline()
    runpod.serverless.start({"handler": handler})


if __name__ == "__main__":  # pragma: no cover
    main()
