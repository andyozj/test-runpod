"""Validation and dimension snapping."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from worker.schemas import GenerationRequest, snap_to_multiple


@pytest.mark.parametrize(
    ("value", "expected"),
    [(256, 256), (1000, 992), (1023, 1008), (1024, 1024), (1536, 1536)],
)
def test_snap_to_multiple_rounds_down_to_sixteen(value: int, expected: int) -> None:
    assert snap_to_multiple(value) == expected


def test_request_requires_only_a_prompt() -> None:
    request = GenerationRequest(prompt="a red fox")

    assert request.width == 1024
    assert request.num_inference_steps == 28
    assert request.seed is None


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t "])
def test_blank_prompt_is_rejected(prompt: str) -> None:
    with pytest.raises(ValidationError):
        GenerationRequest(prompt=prompt)


def test_prompt_at_the_cap_is_accepted() -> None:
    assert GenerationRequest(prompt="a" * 2000).prompt


def test_prompt_over_the_cap_is_rejected() -> None:
    with pytest.raises(ValidationError):
        GenerationRequest(prompt="a" * 2001)


@pytest.mark.parametrize(("width", "height"), [(255, 1024), (1024, 1537)])
def test_dimensions_outside_bounds_are_rejected(width: int, height: int) -> None:
    with pytest.raises(ValidationError):
        GenerationRequest(prompt="x", width=width, height=height)


@pytest.mark.parametrize("steps", [0, 51])
def test_steps_outside_bounds_are_rejected(steps: int) -> None:
    with pytest.raises(ValidationError):
        GenerationRequest(prompt="x", num_inference_steps=steps)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        GenerationRequest(prompt="x", negative_prompt="blurry")


def test_effective_dimensions_are_snapped() -> None:
    request = GenerationRequest(prompt="x", width=1000, height=1300)

    assert request.effective_width == 992
    assert request.effective_height == 1296
