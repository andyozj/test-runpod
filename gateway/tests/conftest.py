"""Hand-written recording fakes for every protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gateway.adapters.memory import InMemoryJobRepository
from gateway.core.models import ErrorCode, JobResult, JobStatus, Progress
from gateway.core.protocols import EndpointHealth, RunPodJobStatus
from gateway.core.service import JobService
from gateway.settings import Settings

FROZEN = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)


@dataclass
class FrozenClock:
    at: datetime = FROZEN

    def now(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at = self.at + timedelta(seconds=seconds)


@dataclass
class FakeRunPodClient:
    """Records submissions; returns scripted statuses."""

    submissions: list[dict[str, Any]] = field(default_factory=list)
    next_status: RunPodJobStatus | None = None
    health_value: EndpointHealth | None = None
    submit_raises: Exception | None = None
    status_raises: Exception | None = None
    health_raises: Exception | None = None
    _counter: int = 0

    async def submit(self, payload: dict[str, Any]) -> str:
        if self.submit_raises is not None:
            raise self.submit_raises
        self.submissions.append(payload)
        self._counter += 1
        return f"runpod-{self._counter}"

    async def status(self, runpod_job_id: str) -> RunPodJobStatus:
        if self.status_raises is not None:
            raise self.status_raises
        return self.next_status or RunPodJobStatus(status=JobStatus.IN_PROGRESS)

    async def health(self) -> EndpointHealth:
        if self.health_raises is not None:
            raise self.health_raises
        return self.health_value or EndpointHealth(0, 0, 0, 1)


@dataclass
class Verdict:
    blocked: bool = False
    reason: str | None = None
    categories: tuple[str, ...] = ()


@dataclass
class FakeGuardrail:
    verdict: Verdict = field(default_factory=Verdict)
    seen: list[str] = field(default_factory=list)

    def check(self, prompt: str) -> Verdict:
        self.seen.append(prompt)
        return self.verdict


def completed(seed: int = 42) -> RunPodJobStatus:
    return RunPodJobStatus(
        status=JobStatus.COMPLETED,
        result=JobResult(
            image_base64="aGVsbG8=",
            storage_key=None,
            format="png",
            seed=seed,
            width=1024,
            height=1024,
            model_version="black-forest-labs/FLUX.1-dev@0ef5fff",
            inference_seconds=21.4,
        ),
    )


def failed(code: ErrorCode = ErrorCode.INFERENCE_FAILED) -> RunPodJobStatus:
    return RunPodJobStatus(
        status=JobStatus.FAILED, error_code=code, error_message="boom"
    )


def in_progress(step: int, total: int) -> RunPodJobStatus:
    return RunPodJobStatus(
        status=JobStatus.IN_PROGRESS,
        progress=Progress(step=step, total=total, percent=round(100 * step / total)),
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def repository(clock: FrozenClock) -> InMemoryJobRepository:
    return InMemoryJobRepository(clock=clock)


@pytest.fixture
def runpod() -> FakeRunPodClient:
    return FakeRunPodClient()


@pytest.fixture
def guardrail() -> FakeGuardrail:
    return FakeGuardrail()


@pytest.fixture
def service(
    repository: InMemoryJobRepository,
    runpod: FakeRunPodClient,
    guardrail: FakeGuardrail,
    clock: FrozenClock,
) -> JobService:
    return JobService(
        repository=repository, runpod=runpod, guardrail=guardrail, clock=clock
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(gateway_api_keys="demo:secret-key,other:other-key")
