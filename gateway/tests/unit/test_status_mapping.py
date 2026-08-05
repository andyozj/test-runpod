"""RunPod vocabulary maps to exactly one domain status."""

from __future__ import annotations

import pytest

from gateway.adapters.runpod_client import map_status
from gateway.core.models import JobStatus


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("IN_QUEUE", JobStatus.QUEUED),
        ("IN_PROGRESS", JobStatus.IN_PROGRESS),
        ("COMPLETED", JobStatus.COMPLETED),
        ("FAILED", JobStatus.FAILED),
        ("CANCELLED", JobStatus.CANCELLED),
        ("TIMED_OUT", JobStatus.TIMED_OUT),
    ],
)
def test_every_known_status_maps(raw: str, expected: JobStatus) -> None:
    assert map_status(raw) is expected


def test_unknown_status_raises_rather_than_defaulting() -> None:
    with pytest.raises(ValueError, match="unknown RunPod status"):
        map_status("SOMETHING_NEW")


def test_terminal_states_are_marked_terminal() -> None:
    assert JobStatus.COMPLETED.terminal
    assert JobStatus.BLOCKED.terminal
    assert not JobStatus.QUEUED.terminal
    assert not JobStatus.IN_PROGRESS.terminal
