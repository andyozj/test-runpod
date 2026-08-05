"""The HTTP surface, end to end against fakes."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from gateway.api.app import Deps, create_app
from gateway.core.service import JobService
from gateway.settings import Settings
from tests.conftest import FakeGuardrail, FakeRunPodClient, Verdict, completed

AUTH = {"Authorization": "Bearer secret-key"}


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
