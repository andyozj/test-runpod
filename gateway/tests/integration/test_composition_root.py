"""The composition root wires real implementations together.

Worth testing rather than excluding: this is the one module that names both a
protocol and an implementation, so it is where wiring breaks silently.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway import main
from gateway.adapters.images import FilesystemImageStore
from gateway.adapters.memory import InMemoryJobRepository
from gateway.adapters.postgres import PostgresJobRepository
from gateway.adapters.ratelimit import TokenBucketRateLimiter
from gateway.adapters.runpod_client import submit_envelope_s
from gateway.core.service import JobService
from gateway.main import (
    REQUEST_TIMEOUT_S,
    SUBMIT_MAX_ATTEMPTS,
    build,
    submit_grace_s,
)
from gateway.settings import Settings
from tests.conftest import FrozenClock, make_job


def _settings() -> Settings:
    return Settings(
        runpod_api_key="test-key",
        runpod_endpoint_id="test-endpoint",
        gateway_api_keys="demo:secret-key",
        reconcile_interval_s=3600,
        reconcile_idle_interval_s=3600,
    )


def test_the_submit_grace_covers_the_worst_case_submit() -> None:
    """Below the retry envelope the reconciler adopts a live submit and duplicates it."""
    envelope = submit_envelope_s(SUBMIT_MAX_ATTEMPTS, REQUEST_TIMEOUT_S)

    assert _settings().submit_grace_s < envelope  # the configured floor alone is short
    assert submit_grace_s(_settings()) == envelope


def test_a_configured_grace_below_the_envelope_is_raised_to_it() -> None:
    settings = _settings()
    settings.submit_grace_s = 1.0

    assert submit_grace_s(settings) == submit_envelope_s(
        SUBMIT_MAX_ATTEMPTS, REQUEST_TIMEOUT_S
    )


def test_a_configured_grace_above_the_envelope_is_kept() -> None:
    settings = _settings()
    settings.submit_grace_s = 600.0

    assert submit_grace_s(settings) == 600.0


async def test_a_submit_still_inside_the_envelope_is_not_adoptable(
    clock: FrozenClock,
) -> None:
    """The boundary the derived grace exists to hold, at the derived value."""
    grace = submit_grace_s(_settings())
    repository = InMemoryJobRepository(clock=clock)
    await repository.create(make_job())

    clock.advance(grace - 1)
    inside = await repository.claim_unresolved(10, lease_s=60.0, submit_grace_s=grace)
    clock.advance(2)
    outside = await repository.claim_unresolved(10, lease_s=60.0, submit_grace_s=grace)

    assert inside == []
    assert len(outside) == 1


def test_the_service_is_wired_with_the_derived_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The derivation is worthless if the composition root passes the raw setting."""
    captured: list[float] = []
    real = main.JobService

    def record(**kwargs: Any) -> JobService:
        captured.append(kwargs["submit_grace_s"])
        return real(**kwargs)

    monkeypatch.setattr(main, "JobService", record)
    build(_settings())

    assert captured == [submit_grace_s(_settings())]


def test_app_builds_and_serves_health() -> None:
    with TestClient(build(_settings())) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_real_blocklist_is_wired_in() -> None:
    with TestClient(build(_settings())) as client:
        created = client.post(
            "/v1/jobs",
            json={"prompt": "a study in gore and mutilation"},
            headers={"Authorization": "Bearer secret-key"},
        )
        job = client.get(
            f"/v1/jobs/{created.json()['job_id']}",
            headers={"Authorization": "Bearer secret-key"},
        )

    assert job.json()["status"] == "BLOCKED"
    assert job.json()["error"]["code"] == "PROMPT_BLOCKED"


def test_openapi_schema_is_generated() -> None:
    with TestClient(build(_settings())) as client:
        schema = client.get("/openapi.json").json()

    assert "/v1/jobs" in schema["paths"]
    assert "/v1/jobs/{job_id}/image" in schema["paths"]
    assert "/health" in schema["paths"]


def _captured_service_kwargs(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> dict[str, Any]:
    """Build the app while recording what the service was wired with.

    Building never touches the database — migrations and the pool live in the
    lifespan — so a Postgres binding is assertable without a server.
    """
    captured: dict[str, Any] = {}
    real = main.JobService

    def record(**kwargs: Any) -> JobService:
        captured.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(main, "JobService", record)
    build(settings)
    return captured


def test_the_rate_limiter_is_wired_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    settings.gateway_rate_limit_rpm = 7
    settings.gateway_rate_limit_burst = 3

    captured = _captured_service_kwargs(monkeypatch, settings)

    limiter = captured["rate_limiter"]
    assert isinstance(limiter, TokenBucketRateLimiter)
    assert limiter.rpm == 7
    assert limiter.burst == 3


def test_no_database_url_binds_memory_and_no_image_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _captured_service_kwargs(monkeypatch, _settings())

    assert isinstance(captured["repository"], InMemoryJobRepository)
    assert captured["image_store"] is None


def test_a_database_url_binds_postgres_and_the_filesystem_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    settings = _settings()
    settings.database_url = "postgresql://gateway:secret@localhost:5432/gateway"
    settings.gateway_image_dir = str(tmp_path / "images")
    settings.gateway_image_store_max_bytes = 1234

    captured = _captured_service_kwargs(monkeypatch, settings)

    repository = captured["repository"]
    store = captured["image_store"]
    assert isinstance(repository, PostgresJobRepository)
    assert repository.dsn == settings.database_url
    assert isinstance(store, FilesystemImageStore)
    assert str(store.root) == settings.gateway_image_dir
    assert store.max_bytes == 1234
