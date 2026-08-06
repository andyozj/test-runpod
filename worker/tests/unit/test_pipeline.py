"""Pipeline accessor memoisation. No GPU."""

from __future__ import annotations

from typing import Any

import pytest

from worker import pipeline as pipeline_module


class _FakePipeline:
    def __call__(self, **kwargs: Any) -> Any:  # noqa: ANN401
        raise NotImplementedError


def test_get_pipeline_memoises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def _fake_load() -> _FakePipeline:
        calls.append(1)
        return _FakePipeline()

    monkeypatch.setattr(pipeline_module, "_load_pipeline", _fake_load)
    monkeypatch.setattr(pipeline_module, "_pipeline", None)

    first = pipeline_module.get_pipeline()
    second = pipeline_module.get_pipeline()

    assert first is second
    assert len(calls) == 1
