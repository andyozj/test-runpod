"""The HTTP surface, end to end against fakes."""

from __future__ import annotations

import base64
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.adapters.memory import InMemoryJobRepository
from gateway.api.app import RECONCILER_STALL_S, Deps, create_app
from gateway.core.protocols import EndpointHealth, UpstreamUnavailableError
from gateway.core.service import JobService
from gateway.settings import Settings
from tests.conftest import (
    FakeGuardrail,
    FakeRunPodClient,
    FrozenClock,
    Verdict,
    completed,
)

AUTH = {"Authorization": "Bearer secret-key"}
# The `settings` fixture configures a second caller; every cross-tenant check
# needs a real credential, not an anonymous one.
OTHER_AUTH = {"Authorization": "Bearer other-key"}
UNKNOWN_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


@pytest.fixture
def client(service: JobService, settings: Settings) -> TestClient:
    return TestClient(create_app(Deps(service=service, settings=settings)))


def test_submit_returns_202_and_a_job_id(client: TestClient) -> None:
    response = client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=AUTH)

    assert response.status_code == 202
    assert response.json()["status"] == "QUEUED"
    assert response.headers["X-Correlation-ID"]


def test_missing_credential_is_401(client: TestClient) -> None:
    response = client.post("/v1/jobs", json={"prompt": "x"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_invalid_credential_is_401(client: TestClient) -> None:
    response = client.post(
        "/v1/jobs", json={"prompt": "x"}, headers={"Authorization": "Bearer nope"}
    )

    assert response.status_code == 401


def test_validation_failure_is_400_with_our_envelope(client: TestClient) -> None:
    response = client.post("/v1/jobs", json={"prompt": "x", "width": 99}, headers=AUTH)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_DIMENSIONS"
    assert response.json()["error"]["suggestion"]


def test_blank_prompt_is_400(client: TestClient) -> None:
    response = client.post("/v1/jobs", json={"prompt": "  "}, headers=AUTH)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_PROMPT"


def test_unknown_field_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/jobs", json={"prompt": "x", "negative_prompt": "blurry"}, headers=AUTH
    )

    assert response.status_code == 400


def test_unknown_job_is_404(client: TestClient) -> None:
    response = client.get("/v1/jobs/00000000-0000-0000-0000-000000000000", headers=AUTH)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_idempotency_replay_returns_200_and_the_original(client: TestClient) -> None:
    headers = {**AUTH, "Idempotency-Key": "abc"}
    first = client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=headers)
    second = client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.headers["Idempotency-Replayed"] == "true"
    assert second.json()["job_id"] == first.json()["job_id"]


def test_replay_is_detected_even_on_the_original_correlation_id(
    client: TestClient,
) -> None:
    """A client retrying with its own trace id is still a replay."""
    headers = {
        **AUTH,
        "Idempotency-Key": "same-trace",
        "X-Correlation-ID": "trace-1",
    }
    client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=headers)
    second = client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=headers)

    assert second.status_code == 200
    assert second.headers["Idempotency-Replayed"] == "true"


def test_idempotency_conflict_is_409(client: TestClient) -> None:
    headers = {**AUTH, "Idempotency-Key": "abc"}
    client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=headers)
    response = client.post("/v1/jobs", json={"prompt": "a blue fox"}, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_blocked_prompt_is_a_200_job_not_an_http_error(
    client: TestClient, guardrail: FakeGuardrail
) -> None:
    guardrail.verdict = Verdict(blocked=True, reason="nope")

    created = client.post("/v1/jobs", json={"prompt": "x"}, headers=AUTH)
    job = client.get(f"/v1/jobs/{created.json()['job_id']}", headers=AUTH)

    assert job.status_code == 200
    assert job.json()["status"] == "BLOCKED"
    assert job.json()["error"]["code"] == "PROMPT_BLOCKED"


async def test_completed_job_exposes_its_result(
    client: TestClient, service: JobService, runpod: FakeRunPodClient
) -> None:
    created = client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=AUTH)
    runpod.next_status = completed(seed=99)
    await service.reconcile()

    view = client.get(f"/v1/jobs/{created.json()['job_id']}", headers=AUTH)

    assert view.json()["status"] == "COMPLETED"
    assert view.json()["result"]["seed"] == 99
    assert base64.b64decode(view.json()["result"]["image_base64"])


def test_cancel_stops_the_job_upstream(
    client: TestClient, runpod: FakeRunPodClient
) -> None:
    created = client.post("/v1/jobs", json={"prompt": "x"}, headers=AUTH)
    job_id = created.json()["job_id"]

    response = client.post(f"/v1/jobs/{job_id}/cancel", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert runpod.cancelled == ["runpod-1"]


async def test_cancelling_a_completed_job_keeps_its_result(
    client: TestClient, service: JobService, runpod: FakeRunPodClient
) -> None:
    created = client.post("/v1/jobs", json={"prompt": "x"}, headers=AUTH)
    runpod.next_status = completed()
    await service.reconcile()
    job_id = created.json()["job_id"]

    response = client.post(f"/v1/jobs/{job_id}/cancel", headers=AUTH)

    assert response.json()["status"] == "COMPLETED"
    assert runpod.cancelled == []


def test_cancelling_an_unknown_job_is_404(client: TestClient) -> None:
    response = client.post(
        "/v1/jobs/00000000-0000-0000-0000-000000000000/cancel", headers=AUTH
    )

    assert response.status_code == 404


def test_correlation_id_is_echoed(client: TestClient) -> None:
    response = client.post(
        "/v1/jobs",
        json={"prompt": "x"},
        headers={**AUTH, "X-Correlation-ID": "trace-42"},
    )

    assert response.headers["X-Correlation-ID"] == "trace-42"


def test_health_needs_no_credential(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_detailed_health_requires_a_credential(client: TestClient) -> None:
    assert client.get("/health/detailed").status_code == 401
    assert client.get("/health/detailed", headers=AUTH).status_code == 200


def test_active_job_cap_is_429_with_retry_after(
    repository: InMemoryJobRepository,
    runpod: FakeRunPodClient,
    guardrail: FakeGuardrail,
    clock: FrozenClock,
    settings: Settings,
) -> None:
    capped = JobService(
        repository=repository,
        runpod=runpod,
        guardrail=guardrail,
        clock=clock,
        max_active_jobs_per_key=1,
    )
    client = TestClient(create_app(Deps(service=capped, settings=settings)))
    client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=AUTH)

    response = client.post("/v1/jobs", json={"prompt": "a second fox"}, headers=AUTH)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "QUEUE_SATURATED"
    assert response.headers["Retry-After"]


# --- Load shedding and upstream failure, as the caller sees them ---


async def test_queue_saturation_is_429_with_the_estimated_wait(
    client: TestClient, service: JobService, runpod: FakeRunPodClient
) -> None:
    """The header must carry the wait the service computed, not a fixed guess."""
    runpod.health_value = EndpointHealth(
        in_queue=100, in_progress=0, workers_running=0, workers_idle=1
    )
    await service.reconcile()

    response = client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=AUTH)

    # (100 queued / 1 worker) * 22.0s avg = 2200s, +1 so the caller waits past it.
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "2201"
    assert response.json()["error"]["code"] == "QUEUE_SATURATED"
    assert response.json()["error"]["suggestion"] == "Retry after 2201s."
    assert response.json()["error"]["correlation_id"]


async def test_a_shed_submission_is_never_dispatched_upstream(
    client: TestClient, service: JobService, runpod: FakeRunPodClient
) -> None:
    """Shedding that still pays for a GPU job sheds nothing."""
    runpod.health_value = EndpointHealth(
        in_queue=100, in_progress=0, workers_running=0, workers_idle=1
    )
    await service.reconcile()

    client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=AUTH)

    assert runpod.submissions == []


def test_unreachable_upstream_is_503_with_retry_after(
    client: TestClient, runpod: FakeRunPodClient
) -> None:
    runpod.submit_raises = UpstreamUnavailableError("endpoint down")

    response = client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=AUTH)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    body = response.json()["error"]
    assert body["code"] == "UPSTREAM_UNAVAILABLE"
    assert body["message"] == "The endpoint is unreachable."
    assert body["suggestion"] == "Retry with the same Idempotency-Key."


def test_the_503_correlation_id_is_the_callers_own(
    client: TestClient, runpod: FakeRunPodClient
) -> None:
    """Without it the caller cannot correlate its retry with the failure."""
    runpod.submit_raises = UpstreamUnavailableError("endpoint down")

    response = client.post(
        "/v1/jobs",
        json={"prompt": "a red fox"},
        headers={**AUTH, "X-Correlation-ID": "trace-503"},
    )

    assert response.json()["error"]["correlation_id"] == "trace-503"


# --- Cross-tenant isolation ---


def test_another_callers_job_is_404_on_read(client: TestClient) -> None:
    """403 would confirm the id exists, which is itself the leak."""
    created = client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=AUTH)
    job_id = created.json()["job_id"]

    response = client.get(f"/v1/jobs/{job_id}", headers=OTHER_AUTH)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_another_callers_job_is_404_on_cancel(
    client: TestClient, runpod: FakeRunPodClient
) -> None:
    created = client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=AUTH)
    job_id = created.json()["job_id"]

    response = client.post(f"/v1/jobs/{job_id}/cancel", headers=OTHER_AUTH)

    assert response.status_code == 404
    assert runpod.cancelled == []


def test_a_cross_tenant_read_is_indistinguishable_from_an_unknown_id(
    client: TestClient,
) -> None:
    created = client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=AUTH)
    job_id = created.json()["job_id"]

    theirs = client.get(f"/v1/jobs/{job_id}", headers=OTHER_AUTH)
    unknown = client.get(f"/v1/jobs/{UNKNOWN_ID}", headers=OTHER_AUTH)

    assert theirs.status_code == unknown.status_code
    assert theirs.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert (
        theirs.json()["error"]["message"].replace(job_id, str(UNKNOWN_ID))
        == (unknown.json()["error"]["message"])
    )


def test_a_cross_tenant_cancel_is_indistinguishable_from_an_unknown_id(
    client: TestClient,
) -> None:
    created = client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=AUTH)
    job_id = created.json()["job_id"]

    theirs = client.post(f"/v1/jobs/{job_id}/cancel", headers=OTHER_AUTH)
    unknown = client.post(f"/v1/jobs/{UNKNOWN_ID}/cancel", headers=OTHER_AUTH)

    assert theirs.status_code == unknown.status_code
    assert theirs.json()["error"]["code"] == unknown.json()["error"]["code"]


def test_the_owner_still_reads_its_own_job(client: TestClient) -> None:
    """The isolation tests above would pass just as well if nobody could read."""
    created = client.post("/v1/jobs", json={"prompt": "a red fox"}, headers=AUTH)

    response = client.get(f"/v1/jobs/{created.json()['job_id']}", headers=AUTH)

    assert response.status_code == 200


# --- /health/detailed ---


def detailed(
    deps_kwargs: dict[str, Any], service: JobService, settings: Settings
) -> dict[str, Any]:
    app = create_app(Deps(service=service, settings=settings, **deps_kwargs))
    body: dict[str, Any] = TestClient(app).get("/health/detailed", headers=AUTH).json()
    return body


def test_detailed_health_reports_unknown_before_any_reading(
    service: JobService, settings: Settings
) -> None:
    body = detailed({}, service, settings)

    assert body["status"] == "degraded"
    assert body["version"] == settings.version
    assert body["checks"]["runpod"] == {
        "status": "unknown",
        "detail": "no health reading yet",
    }
    assert body["checks"]["reconciler"] == {
        "status": "unknown",
        "detail": "no completed tick yet",
    }


async def test_detailed_health_reports_the_cached_queue_and_worker_counts(
    service: JobService, runpod: FakeRunPodClient, settings: Settings
) -> None:
    runpod.health_value = EndpointHealth(
        in_queue=3, in_progress=1, workers_running=2, workers_idle=4
    )
    await service.reconcile()

    body = detailed({"reconciler_age": lambda: 1.5}, service, settings)

    assert body["status"] == "ok"
    assert body["checks"]["runpod"] == {
        "status": "ok",
        "in_queue": 3,
        "in_progress": 1,
        "workers_running": 2,
        "workers_idle": 4,
    }
    assert body["checks"]["reconciler"] == {"status": "ok", "last_tick_s": 1.5}


async def test_a_stalled_reconciler_degrades_detailed_health(
    service: JobService, runpod: FakeRunPodClient, settings: Settings
) -> None:
    """A dead loop leaves the process healthy and every job unresolvable."""
    await service.reconcile()
    age = RECONCILER_STALL_S + 1

    body = detailed({"reconciler_age": lambda: age}, service, settings)

    assert body["status"] == "degraded"
    assert body["checks"]["reconciler"] == {"status": "stalled", "last_tick_s": age}
    assert body["checks"]["runpod"]["status"] == "ok"


async def test_an_age_at_the_stall_threshold_is_still_ok(
    service: JobService, runpod: FakeRunPodClient, settings: Settings
) -> None:
    await service.reconcile()

    body = detailed({"reconciler_age": lambda: RECONCILER_STALL_S}, service, settings)

    assert body["status"] == "ok"
    assert body["checks"]["reconciler"]["status"] == "ok"


async def test_an_unreachable_endpoint_degrades_detailed_health(
    service: JobService, runpod: FakeRunPodClient, settings: Settings
) -> None:
    runpod.health_raises = RuntimeError("endpoint down")
    await service.reconcile()

    body = detailed({"reconciler_age": lambda: 1.0}, service, settings)

    assert body["status"] == "degraded"
    assert body["checks"]["runpod"]["status"] == "unknown"
