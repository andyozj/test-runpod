"""Unit tests for the pure functions in scripts/apply_endpoint.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apply_endpoint import (  # noqa: E402 - path setup must precede this import
    endpoint_body,
    is_latest_tag,
    template_body,
)


def test_is_latest_tag_refuses_bare_latest() -> None:
    assert is_latest_tag("latest") is True


def test_is_latest_tag_refuses_repo_qualified_latest() -> None:
    assert is_latest_tag("ghcr.io/owner/flux-worker:latest") is True


def test_is_latest_tag_accepts_immutable_tag() -> None:
    assert is_latest_tag("0.1.0-a3f21c8-slim") is False


def test_template_body_maps_config_to_payload() -> None:
    config = {
        "name": "flux-worker",
        "image_repository": "ghcr.io/owner/flux-worker",
        "env": {"WEIGHTS_PATH": "/opt/weights"},
        "container_disk_gb": 40,
    }

    body = template_body(config, "0.1.0-a3f21c8-slim")

    assert body == {
        "name": "flux-worker-template",
        "imageName": "ghcr.io/owner/flux-worker:0.1.0-a3f21c8-slim",
        "env": {"WEIGHTS_PATH": "/opt/weights"},
        "containerDiskInGb": 40,
        "isServerless": True,
    }


def test_template_body_defaults_container_disk_and_env() -> None:
    config = {"name": "flux-worker", "image_repository": "ghcr.io/owner/flux-worker"}

    body = template_body(config, "0.1.0-a3f21c8-slim")

    assert body["env"] == {}
    assert body["containerDiskInGb"] == 20


def test_endpoint_body_maps_scaling_and_gpu_fields() -> None:
    config = {
        "name": "flux-worker",
        "gpu_types": ["NVIDIA L40S", "NVIDIA A100 80GB PCIe"],
        "workers": {"min": 0, "max": 3},
        "idle_timeout_s": 60,
        "execution_timeout_ms": 300000,
        "scaler_type": "QUEUE_DELAY",
        "scaler_value": 4,
        "flashboot": True,
        "allowed_cuda_versions": ["13.0", "12.9"],
    }

    body = endpoint_body(config, template_id="tmpl-1", endpoint_id=None)

    assert body["name"] == "flux-worker"
    assert body["templateId"] == "tmpl-1"
    assert body["gpuTypeIds"] == ["NVIDIA L40S", "NVIDIA A100 80GB PCIe"]
    assert body["workersMin"] == 0
    assert body["workersMax"] == 3
    assert body["idleTimeout"] == 60
    assert body["executionTimeoutMs"] == 300000
    assert body["allowedCudaVersions"] == ["13.0", "12.9"]
    assert "id" not in body


def test_endpoint_body_includes_id_when_updating_existing() -> None:
    config = {"name": "flux-worker"}

    body = endpoint_body(config, template_id="tmpl-1", endpoint_id="ep-1")

    assert body["id"] == "ep-1"


def test_endpoint_body_defaults_when_workers_and_cuda_absent() -> None:
    config = {"name": "flux-worker"}

    body = endpoint_body(config, template_id="tmpl-1", endpoint_id=None)

    assert body["workersMin"] == 0
    assert body["workersMax"] == 1
    assert "allowedCudaVersions" not in body
