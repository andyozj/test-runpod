"""Handler success and every error path, against fakes. No GPU."""

from __future__ import annotations

import base64
import io

from PIL import Image

from tests.conftest import (
    FakePipeline,
    OutOfMemoryError,
    RecordingGuardrail,
    RecordingImageGuardrail,
)
from worker import handler as handler_module
from worker.guardrails import GuardrailVerdict
from worker.schemas import GenerationRequest
from worker.settings import Settings


def _envelope(output: dict[str, object]) -> dict[str, object]:
    """Decode the JSON-encoded error envelope from a job output."""
    import json

    decoded: dict[str, object] = json.loads(str(output["error"]))
    return decoded


def _run(
    job_input: dict[str, object],
    pipeline: FakePipeline,
    settings: Settings,
) -> dict[str, object]:
    """Drive the handler with an injected pipeline, bypassing get_pipeline."""
    original = handler_module._run

    def _patched(job, request, log, **_):  # type: ignore[no-untyped-def]
        return original(job, request, log, pipeline=pipeline, settings=settings)

    handler_module._run = _patched  # type: ignore[assignment]
    try:
        return handler_module.handler({"input": job_input})
    finally:
        handler_module._run = original  # type: ignore[assignment]


def test_successful_job_returns_a_decodable_image(
    pipeline: FakePipeline, settings: Settings
) -> None:
    output = _run({"prompt": "a red fox", "seed": 42}, pipeline, settings)

    assert output["seed"] == 42
    assert output["model_version"] == "black-forest-labs/FLUX.1-dev@0ef5fff"
    decoded = base64.b64decode(str(output["image_base64"]))
    assert Image.open(io.BytesIO(decoded)).size == (1024, 1024)


def test_effective_dimensions_are_echoed(
    pipeline: FakePipeline, settings: Settings
) -> None:
    output = _run({"prompt": "x", "width": 1000, "height": 1000}, pipeline, settings)

    assert output["width"] == 992
    assert pipeline.calls[0]["width"] == 992


def test_absent_seed_is_generated_and_echoed(
    pipeline: FakePipeline, settings: Settings
) -> None:
    output = _run({"prompt": "x"}, pipeline, settings)

    assert isinstance(output["seed"], int)


def test_progress_callback_fires_once_per_step(
    pipeline: FakePipeline, settings: Settings
) -> None:
    seen: list[tuple[int, int]] = []
    request = GenerationRequest(prompt="x", num_inference_steps=4)

    from worker.inference import generate

    generate(request, pipeline, settings, on_progress=lambda s, t: seen.append((s, t)))

    assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_progress_reporting_is_throttled_to_stride() -> None:
    reported: list[int] = []
    last = -handler_module.PROGRESS_STRIDE_PCT
    for step in range(1, 29):
        if handler_module.should_report_progress(step, 28, last):
            last = round(100 * step / 28)
            reported.append(step)

    assert len(reported) <= 100 // handler_module.PROGRESS_STRIDE_PCT + 1
    assert reported[0] == 1
    assert reported[-1] == 28


def test_progress_final_step_always_reports() -> None:
    assert handler_module.should_report_progress(4, 4, last_percent=100)


def test_oom_sets_refresh_worker(settings: Settings) -> None:
    pipeline = FakePipeline(raises=OutOfMemoryError("CUDA out of memory"))

    output = _run({"prompt": "x"}, pipeline, settings)

    assert output["refresh_worker"] is True
    assert _envelope(output)["code"] == "OOM"  # type: ignore[index]


def test_generic_failure_does_not_leak_internal_detail(settings: Settings) -> None:
    pipeline = FakePipeline(raises=RuntimeError("cudnn kernel /opt/secret/path.so"))

    output = _run({"prompt": "x"}, pipeline, settings)

    assert _envelope(output)["code"] == "INFERENCE_FAILED"  # type: ignore[index]
    assert "refresh_worker" not in output
    assert "secret" not in str(output)


def test_blocked_prompt_never_reaches_the_pipeline(
    pipeline: FakePipeline, settings: Settings
) -> None:
    handler_module.set_guardrails(
        prompt=RecordingGuardrail(
            verdict=GuardrailVerdict(action="block", reason="nope")
        )
    )

    output = _run({"prompt": "anything"}, pipeline, settings)

    assert _envelope(output)["code"] == "PROMPT_BLOCKED"  # type: ignore[index]
    assert pipeline.calls == []


def test_blocked_image_is_reported_and_not_returned(
    pipeline: FakePipeline, settings: Settings
) -> None:
    handler_module.set_guardrails(
        image=RecordingImageGuardrail(
            verdict=GuardrailVerdict(action="block", reason="nope")
        )
    )

    output = _run({"prompt": "x"}, pipeline, settings)

    assert _envelope(output)["code"] == "IMAGE_BLOCKED"  # type: ignore[index]
    assert "image_base64" not in output


def test_image_guardrail_receives_the_generated_bytes(
    pipeline: FakePipeline, settings: Settings
) -> None:
    recorder = RecordingImageGuardrail()
    handler_module.set_guardrails(image=recorder)

    _run({"prompt": "x"}, pipeline, settings)

    assert len(recorder.seen) == 1


def test_invalid_prompt_is_rejected_before_the_pipeline(
    pipeline: FakePipeline, settings: Settings
) -> None:
    output = _run({"prompt": "   "}, pipeline, settings)

    assert _envelope(output)["code"] == "INVALID_PROMPT"  # type: ignore[index]
    assert pipeline.calls == []


def test_invalid_dimensions_map_to_their_own_code(
    pipeline: FakePipeline, settings: Settings
) -> None:
    output = _run({"prompt": "x", "width": 99}, pipeline, settings)

    assert _envelope(output)["code"] == "INVALID_DIMENSIONS"  # type: ignore[index]


def test_invalid_steps_map_to_their_own_code(
    pipeline: FakePipeline, settings: Settings
) -> None:
    output = _run({"prompt": "x", "num_inference_steps": 99}, pipeline, settings)

    assert _envelope(output)["code"] == "INVALID_STEPS"  # type: ignore[index]


def test_errors_carry_a_suggestion(pipeline: FakePipeline, settings: Settings) -> None:
    output = _run({"prompt": ""}, pipeline, settings)

    assert _envelope(output)["suggestion"]


def test_missing_input_key_is_handled(
    pipeline: FakePipeline, settings: Settings
) -> None:
    assert "error" in handler_module.handler({})
