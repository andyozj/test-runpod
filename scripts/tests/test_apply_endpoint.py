"""Unit tests for scripts/apply_endpoint.py. HTTP is stubbed; nothing hits the network."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import apply_endpoint  # noqa: E402 - path setup must precede this import
from apply_endpoint import (  # noqa: E402
    endpoint_body,
    find_endpoint,
    is_latest_tag,
    patch_body,
)


def test_is_latest_tag_refuses_bare_latest() -> None:
    assert is_latest_tag("latest") is True


def test_is_latest_tag_refuses_repo_qualified_latest() -> None:
    assert is_latest_tag("ghcr.io/owner/flux-worker:latest") is True


def test_is_latest_tag_accepts_immutable_tag() -> None:
    assert is_latest_tag("0.1.0-a3f21c8-slim") is False


def test_endpoint_body_maps_config_to_v2_payload() -> None:
    config = {
        "name": "flux-worker",
        "image_repository": "ghcr.io/owner/flux-worker",
        "gpu_pools": ["ADA_48_PRO"],
        "workers": {"min": 0, "max": 3},
        "idle_timeout_s": 60,
        "execution_timeout_ms": 300000,
        "scaler_type": "QUEUE_DELAY",
        "scaler_value": 4,
        "flashboot": True,
        "container_disk_gb": 20,
        "env": {"MODEL_CACHE_ROOT": "/runpod-volume/huggingface-cache/hub"},
    }

    body = endpoint_body(config, "0.1.0-a3f21c8-slim")

    assert body == {
        "name": "flux-worker",
        "image": "ghcr.io/owner/flux-worker:0.1.0-a3f21c8-slim",
        "env": {"MODEL_CACHE_ROOT": "/runpod-volume/huggingface-cache/hub"},
        "disk": 20,
        "type": "QUEUE",
        "gpu": {"pools": ["ADA_48_PRO"], "count": 1},
        "workers": {"min": 0, "max": 3, "idleTimeout": 60},
        "scaling": {"type": "QUEUE_DELAY", "queueDelay": 4},
        "timeout": 300000,
        "flashboot": "FLASHBOOT",
    }


def test_endpoint_body_defaults() -> None:
    config = {"name": "flux-worker", "image_repository": "ghcr.io/owner/flux-worker"}

    body = endpoint_body(config, "0.1.0-a3f21c8-slim")

    assert body["env"] == {}
    assert body["disk"] == 20
    assert body["gpu"] == {"pools": [], "count": 1}
    assert body["workers"] == {"min": 0, "max": 1, "idleTimeout": 5}
    assert body["scaling"] == {"type": "QUEUE_DELAY", "queueDelay": 4}
    assert body["timeout"] == 600000
    assert body["flashboot"] == "FLASHBOOT"


def test_endpoint_body_flashboot_off_maps_to_enum() -> None:
    config = {
        "name": "flux-worker",
        "image_repository": "ghcr.io/owner/flux-worker",
        "flashboot": False,
    }

    assert endpoint_body(config, "0.1.0-a3f21c8-slim")["flashboot"] == "OFF"


def test_endpoint_body_request_count_scaling_omits_idle_timeout() -> None:
    config = {
        "name": "flux-worker",
        "image_repository": "ghcr.io/owner/flux-worker",
        "scaler_type": "REQUEST_COUNT",
        "scaler_value": 2,
        "idle_timeout_s": 60,
    }

    body = endpoint_body(config, "0.1.0-a3f21c8-slim")

    assert body["scaling"] == {"type": "REQUEST_COUNT", "requestCount": 2}
    # v2 rejects idleTimeout for endpoints scaling on requestCount.
    assert "idleTimeout" not in body["workers"]


def test_patch_body_drops_create_only_fields() -> None:
    body = endpoint_body(
        {"name": "flux-worker", "image_repository": "ghcr.io/owner/flux-worker"},
        "0.1.0-a3f21c8-slim",
    )

    patch = patch_body(body)

    assert "type" not in patch
    assert set(patch) == {
        "name",
        "image",
        "env",
        "disk",
        "gpu",
        "workers",
        "scaling",
        "timeout",
        "flashboot",
    }


def test_find_endpoint_matches_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, path: str, api_key: str, body: Any = None) -> Any:
        assert (method, path) == ("GET", "/serverless")
        return {"endpoints": [{"id": "ep-1", "name": "flux-worker-cached"}]}

    monkeypatch.setattr(apply_endpoint, "_request", fake_request)

    endpoint = find_endpoint("flux-worker-cached", "sk-test")

    assert endpoint is not None
    assert endpoint["id"] == "ep-1"


def test_find_endpoint_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(apply_endpoint, "_request", lambda *a, **k: {"endpoints": []})

    assert find_endpoint("flux-worker-cached", "sk-test") is None


def test_find_endpoint_tolerates_bare_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        apply_endpoint,
        "_request",
        lambda *a, **k: [{"id": "ep-1", "name": "flux-worker-cached"}],
    )

    endpoint = find_endpoint("flux-worker-cached", "sk-test")

    assert endpoint is not None
    assert endpoint["id"] == "ep-1"


CONFIG = {
    "name": "flux-worker-cached",
    "image_repository": "ghcr.io/owner/flux-worker",
    "gpu_pools": ["ADA_48_PRO"],
    "workers": {"min": 0, "max": 3},
    "idle_timeout_s": 60,
}


def _record_requests(
    monkeypatch: pytest.MonkeyPatch, endpoints: list[dict[str, Any]]
) -> list[tuple[str, str, Any]]:
    calls: list[tuple[str, str, Any]] = []

    def fake_request(method: str, path: str, api_key: str, body: Any = None) -> Any:
        calls.append((method, path, body))
        if (method, path) == ("GET", "/serverless"):
            return {"endpoints": endpoints}
        if (method, path) == ("POST", "/serverless"):
            return {"id": "ep-9"}
        return None

    monkeypatch.setattr(apply_endpoint, "_request", fake_request)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return calls


def test_apply_creates_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_requests(monkeypatch, endpoints=[])

    code = apply_endpoint.apply(CONFIG, "0.1.0-a3f21c8-slim", "sk-test", False, True)

    assert code == 0
    create = next(b for m, p, b in calls if (m, p) == ("POST", "/serverless"))
    assert create["image"] == "ghcr.io/owner/flux-worker:0.1.0-a3f21c8-slim"
    assert create["type"] == "QUEUE"
    assert not any(m == "PATCH" for m, _, _ in calls)


def test_apply_patches_and_bounces_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_requests(
        monkeypatch, endpoints=[{"id": "ep-1", "name": "flux-worker-cached"}]
    )

    code = apply_endpoint.apply(CONFIG, "0.2.0-b4c5d6e-slim", "sk-test", False, True)

    assert code == 0
    patches = [b for m, p, b in calls if (m, p) == ("PATCH", "/serverless/ep-1")]
    assert patches[0]["image"] == "ghcr.io/owner/flux-worker:0.2.0-b4c5d6e-slim"
    assert "type" not in patches[0]
    # The bounce: workers.max to zero, then back to the configured ceiling.
    assert patches[1]["workers"]["max"] == 0
    assert patches[2]["workers"]["max"] == 3
    assert not any(m == "POST" for m, _, _ in calls)


def test_apply_no_bounce_patches_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_requests(
        monkeypatch, endpoints=[{"id": "ep-1", "name": "flux-worker-cached"}]
    )

    apply_endpoint.apply(CONFIG, "0.2.0-b4c5d6e-slim", "sk-test", False, False)

    patches = [b for m, p, b in calls if (m, p) == ("PATCH", "/serverless/ep-1")]
    assert len(patches) == 1


def test_apply_dry_run_calls_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_requests(monkeypatch, endpoints=[])

    code = apply_endpoint.apply(CONFIG, "0.1.0-a3f21c8-slim", "", True, True)

    assert code == 0
    assert calls == []
