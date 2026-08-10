"""JobView and JobSummary rendering: request params on the wire, additive previews."""

from __future__ import annotations

from gateway.api.schemas import JobSummary, JobView
from gateway.core.models import GenerationParams, JobResult, JobStatus, Progress
from tests.conftest import make_job


def test_progress_without_a_preview_keeps_the_three_field_shape() -> None:
    job = make_job(JobStatus.IN_PROGRESS).advanced(
        progress=Progress(step=7, total=28, percent=25)
    )

    view = JobView.of(job)

    assert view.progress == {"step": 7, "total": 28, "percent": 25}


def test_progress_with_a_preview_carries_both_fields() -> None:
    job = make_job(JobStatus.IN_PROGRESS).advanced(
        progress=Progress(
            step=14, total=28, percent=50, preview_b64="ZnJhbWU=", preview_format="jpeg"
        )
    )

    view = JobView.of(job)

    assert view.progress == {
        "step": 14,
        "total": 28,
        "percent": 50,
        "preview_b64": "ZnJhbWU=",
        "preview_format": "jpeg",
    }


def test_no_progress_renders_as_none() -> None:
    assert JobView.of(make_job()).progress is None


def test_the_view_echoes_the_full_request_without_an_unset_seed() -> None:
    job = make_job().advanced(
        params=GenerationParams(
            prompt="a fox",
            width=768,
            height=512,
            num_inference_steps=12,
            guidance_scale=7.0,
        )
    )

    view = JobView.of(job)

    assert view.request == {
        "prompt": "a fox",
        "width": 768,
        "height": 512,
        "num_inference_steps": 12,
        "guidance_scale": 7.0,
        "output_format": "png",
    }


def test_the_view_request_carries_a_fixed_seed() -> None:
    job = make_job().advanced(params=GenerationParams(prompt="a fox", seed=42))

    assert JobView.of(job).request["seed"] == 42


def test_the_summary_carries_the_request_knobs_the_result_does_not() -> None:
    job = make_job(JobStatus.COMPLETED).advanced(
        params=GenerationParams(
            prompt="a fox",
            num_inference_steps=12,
            guidance_scale=7.0,
            output_format="jpeg",
        ),
        result=JobResult(
            image_base64="aW1n",
            format="jpeg",
            seed=7,
            width=1024,
            height=1024,
            model_version="m@r",
            inference_seconds=1.5,
        ),
    )

    summary = JobSummary.of(job)

    assert summary.num_inference_steps == 12
    assert summary.guidance_scale == 7.0
    assert summary.output_format == "jpeg"
    assert summary.seed == 7
