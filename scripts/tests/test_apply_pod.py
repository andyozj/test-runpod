"""Unit tests for scripts/apply_pod.py. HTTP is stubbed; nothing hits the network."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import apply_pod  # noqa: E402 - path setup must precede this import
from apply_pod import (  # noqa: E402
    find_pod,
    is_latest_tag,
    patch_body,
    pod_body,
    resolve_secret_env,
)


def test_is_latest_tag_refuses_bare_latest() -> None:
    assert is_latest_tag("latest") is True


def test_is_latest_tag_refuses_repo_qualified_latest() -> None:
    assert is_latest_tag("ghcr.io/owner/flux-stack:latest") is True


def test_is_latest_tag_accepts_immutable_tag() -> None:
    assert is_latest_tag("0.1.0-a3f21c8") is False


def test_pod_body_maps_config_to_v2_payload() -> None:
    config = {
        "name": "flux-stack",
        "image_repository": "ghcr.io/owner/flux-stack",
        "cloud": "SECURE",
        "cpu": {"flavor": "cpu3g", "vcpu_count": 2},
        "container_disk_gb": 20,
        "volume_gb": 20,
        "volume_mount_path": "/workspace",
        "ports": ["8000/http"],
        "env": {"DATABASE_URL": "postgresql://gateway:gateway@localhost:5432/gateway"},
    }

    body = pod_body(config, "0.1.0-a3f21c8", {"RUNPOD_API_KEY": "sk-test"})

    assert body == {
        "name": "flux-stack",
        "image": "ghcr.io/owner/flux-stack:0.1.0-a3f21c8",
        "cloud": "SECURE",
        "cpu": {"id": "cpu3g", "vcpuCount": 2},
        "disk": 20,
        "ports": ["8000/http"],
        "env": {
            "DATABASE_URL": "postgresql://gateway:gateway@localhost:5432/gateway",
            "RUNPOD_API_KEY": "sk-test",
        },
        "mounts": {"persistent": {"size": 20, "path": "/workspace"}},
    }


def test_pod_body_defaults() -> None:
    config = {"name": "flux-stack", "image_repository": "ghcr.io/owner/flux-stack"}

    body = pod_body(config, "0.1.0-a3f21c8", {})

    assert body["cloud"] == "SECURE"
    assert body["cpu"] == {"id": "cpu3g", "vcpuCount": 2}
    assert body["disk"] == 20
    assert body["ports"] == []
    assert body["env"] == {}
    assert body["mounts"] == {"persistent": {"size": 20, "path": "/workspace"}}


def test_secrets_override_declared_env() -> None:
    config = {
        "name": "flux-stack",
        "image_repository": "ghcr.io/owner/flux-stack",
        "env": {"GATEWAY_API_KEYS": "committed-placeholder"},
    }

    body = pod_body(config, "0.1.0-a3f21c8", {"GATEWAY_API_KEYS": "demo:real"})

    assert body["env"]["GATEWAY_API_KEYS"] == "demo:real"


def test_patch_body_drops_create_only_fields() -> None:
    body = pod_body(
        {"name": "flux-stack", "image_repository": "ghcr.io/owner/flux-stack"},
        "0.1.0-a3f21c8",
        {},
    )

    patch = patch_body(body)

    assert "cpu" not in patch
    assert "cloud" not in patch
    assert set(patch) == {"name", "image", "disk", "ports", "env", "mounts"}


def test_resolve_secret_env_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "sk-test")
    config = {"secret_env": ["RUNPOD_API_KEY"]}

    assert resolve_secret_env(config, dry_run=False) == {"RUNPOD_API_KEY": "sk-test"}


def test_resolve_secret_env_missing_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_API_KEYS", raising=False)
    config = {"secret_env": ["GATEWAY_API_KEYS"]}

    with pytest.raises(SystemExit, match="GATEWAY_API_KEYS"):
        resolve_secret_env(config, dry_run=False)


def test_resolve_secret_env_dry_run_never_reads_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "sk-must-not-leak")

    resolved = resolve_secret_env({"secret_env": ["RUNPOD_API_KEY"]}, dry_run=True)

    assert resolved == {"RUNPOD_API_KEY": "<env:RUNPOD_API_KEY>"}


def test_find_pod_matches_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, path: str, api_key: str, body: Any = None) -> Any:
        assert (method, path) == ("GET", "/pods")
        return {"pods": [{"id": "pod-1", "name": "flux-stack", "status": "RUNNING"}]}

    monkeypatch.setattr(apply_pod, "_request", fake_request)

    pod = find_pod("flux-stack", "sk-test")

    assert pod is not None
    assert pod["id"] == "pod-1"


def test_find_pod_returns_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apply_pod, "_request", lambda *a, **k: {"pods": []})

    assert find_pod("flux-stack", "sk-test") is None


def test_find_pod_tolerates_bare_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        apply_pod, "_request", lambda *a, **k: [{"id": "pod-1", "name": "flux-stack"}]
    )

    pod = find_pod("flux-stack", "sk-test")

    assert pod is not None
    assert pod["id"] == "pod-1"


CONFIG = {
    "name": "flux-stack",
    "image_repository": "ghcr.io/owner/flux-stack",
    "secret_env": ["RUNPOD_API_KEY"],
}


def _record_requests(
    monkeypatch: pytest.MonkeyPatch, pods: list[dict[str, Any]]
) -> list[tuple[str, str, Any]]:
    calls: list[tuple[str, str, Any]] = []

    def fake_request(method: str, path: str, api_key: str, body: Any = None) -> Any:
        calls.append((method, path, body))
        if (method, path) == ("GET", "/pods"):
            return {"pods": pods}
        if (method, path) == ("POST", "/pods"):
            return {"id": "pod-9"}
        return None

    monkeypatch.setattr(apply_pod, "_request", fake_request)
    return calls


def test_apply_creates_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "sk-test")
    calls = _record_requests(monkeypatch, pods=[])

    code = apply_pod.apply(CONFIG, "0.1.0-a3f21c8", "sk-test", False, restart=True)

    assert code == 0
    methods_paths = [(m, p) for m, p, _ in calls]
    assert ("POST", "/pods") in methods_paths
    create = next(b for m, p, b in calls if (m, p) == ("POST", "/pods"))
    assert create["image"] == "ghcr.io/owner/flux-stack:0.1.0-a3f21c8"
    assert not any(m == "PATCH" for m, _, _ in calls)


def test_apply_patches_and_restarts_running_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "sk-test")
    calls = _record_requests(
        monkeypatch, pods=[{"id": "pod-1", "name": "flux-stack", "status": "RUNNING"}]
    )

    code = apply_pod.apply(CONFIG, "0.2.0-b4c5d6e", "sk-test", False, restart=True)

    assert code == 0
    patch = next(b for m, p, b in calls if (m, p) == ("PATCH", "/pods/pod-1"))
    assert patch["image"] == "ghcr.io/owner/flux-stack:0.2.0-b4c5d6e"
    assert "cpu" not in patch
    action = next(b for m, p, b in calls if (m, p) == ("POST", "/pods/pod-1/action"))
    assert action == {"action": "restart"}


def test_apply_starts_exited_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "sk-test")
    calls = _record_requests(
        monkeypatch, pods=[{"id": "pod-1", "name": "flux-stack", "status": "EXITED"}]
    )

    apply_pod.apply(CONFIG, "0.2.0-b4c5d6e", "sk-test", False, restart=True)

    action = next(b for m, p, b in calls if (m, p) == ("POST", "/pods/pod-1/action"))
    assert action == {"action": "start"}


def test_apply_no_restart_skips_action(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "sk-test")
    calls = _record_requests(
        monkeypatch, pods=[{"id": "pod-1", "name": "flux-stack", "status": "RUNNING"}]
    )

    apply_pod.apply(CONFIG, "0.2.0-b4c5d6e", "sk-test", False, restart=False)

    assert not any("/action" in p for _, p, _ in calls)


def test_apply_dry_run_calls_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_requests(monkeypatch, pods=[])

    code = apply_pod.apply(CONFIG, "0.1.0-a3f21c8", "", True, restart=True)

    assert code == 0
    assert calls == []
