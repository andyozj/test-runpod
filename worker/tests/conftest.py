"""Shared fakes. Hand-written recorders, not MagicMock.

A recording fake catches contract drift and makes the negative assertions
possible — "the GPU was never called", "nothing was uploaded" — which are
frequently the assertion that matters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from PIL import Image

from worker.guardrails import GuardrailVerdict
from worker.settings import Settings


@dataclass
class FakeImages:
    images: list[Image.Image]


@dataclass
class FakePipeline:
    """Records every call and returns a solid-colour image."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    raises: Exception | None = None
    colour: str = "red"

    def __call__(self, **kwargs: Any) -> FakeImages:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        callback = kwargs.get("callback_on_step_end")
        if callback is not None:
            for step in range(kwargs["num_inference_steps"]):
                callback(self, step, 0, {})
        size = (kwargs["width"], kwargs["height"])
        return FakeImages(images=[Image.new("RGB", size, self.colour)])


@dataclass
class RecordingGuardrail:
    """Prompt guardrail with a fixed verdict that records what it saw."""

    verdict: GuardrailVerdict = field(default_factory=GuardrailVerdict)
    seen: list[str] = field(default_factory=list)
    raises: Exception | None = None

    def check(self, prompt: str) -> GuardrailVerdict:
        self.seen.append(prompt)
        if self.raises is not None:
            raise self.raises
        return self.verdict


@dataclass
class RecordingImageGuardrail:
    """Image guardrail with a fixed verdict that records what it saw."""

    verdict: GuardrailVerdict = field(default_factory=GuardrailVerdict)
    seen: list[bytes] = field(default_factory=list)
    raises: Exception | None = None

    def check(self, image: bytes) -> GuardrailVerdict:
        self.seen.append(image)
        if self.raises is not None:
            raise self.raises
        return self.verdict


class OutOfMemoryError(Exception):
    """Stands in for torch.cuda.OutOfMemoryError, which is detected by name."""


@pytest.fixture
def settings() -> Settings:
    return Settings(
        model_id="black-forest-labs/FLUX.1-dev",
        model_revision="0ef5fff",
    )


@pytest.fixture
def pipeline() -> FakePipeline:
    return FakePipeline()


@pytest.fixture(autouse=True)
def _reset_handler_guardrails() -> Any:
    from worker import handler as handler_module
    from worker.guardrails import BlocklistPromptGuardrail, NoopImageGuardrail

    yield
    handler_module.set_guardrails(BlocklistPromptGuardrail(), NoopImageGuardrail())
