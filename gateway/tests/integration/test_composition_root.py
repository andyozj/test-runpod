"""The composition root wires real implementations together.

Worth testing rather than excluding: this is the one module that names both a
protocol and an implementation, so it is where wiring breaks silently.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from gateway.main import build
from gateway.settings import Settings


def _settings() -> Settings:
    return Settings(
        runpod_api_key="test-key",
        runpod_endpoint_id="test-endpoint",
        gateway_api_keys="demo:secret-key",
        reconcile_interval_s=3600,
        reconcile_idle_interval_s=3600,
    )


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
    assert "/health" in schema["paths"]
