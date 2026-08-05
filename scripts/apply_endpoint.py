"""Create or update a RunPod serverless endpoint from committed config.

Targets the REST API at `https://rest.runpod.io/v1`, which is RunPod's current
management surface. The GraphQL `saveEndpoint` mutation still works but is
legacy, and it models the problem differently.

REST splits it in two, and the split is worth knowing before reading the code:

    Template  →  image, environment, container disk
    Endpoint  →  GPUs, scaling, timeouts, CUDA filter, templateId

So a deploy is two upserts, not one. Both are idempotent: look up by name,
create if absent, patch if present.

Usage:
    export RUNPOD_API_KEY=...
    python scripts/apply_endpoint.py --config deploy/endpoints/cached.yaml \\
        --tag 0.1.0-a3f21c8-slim --dry-run
    python scripts/apply_endpoint.py --config deploy/endpoints/cached.yaml \\
        --tag 0.1.0-a3f21c8-slim
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "https://rest.runpod.io/v1"
TIMEOUT_S = 30


class ApiError(RuntimeError):
    """A REST call failed."""


def _request(
    method: str, path: str, api_key: str, body: dict[str, Any] | None = None
) -> Any:
    """Call the REST API.

    Args:
        method: HTTP method.
        path: Path below the API base.
        api_key: RunPod API key.
        body: JSON body, if any.

    Returns:
        The decoded response, or None for an empty body.

    Raises:
        ApiError: The call failed, with the response body included — RunPod
            returns useful validation messages and swallowing them turns a
            two-minute fix into a guessing game.
    """
    request = urllib.request.Request(  # noqa: S310 - fixed https host
        f"{BASE_URL}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        msg = f"{method} {path} -> {exc.code} {exc.reason}\n{detail}"
        raise ApiError(msg) from exc


def load_config(path: Path) -> dict[str, Any]:
    """Read an endpoint declaration.

    Args:
        path: The YAML file to read.

    Returns:
        The parsed configuration.
    """
    try:
        import yaml

        loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    except ImportError:
        loaded = _parse_simple_yaml(path.read_text())
    return loaded


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the flat subset of YAML these config files use.

    Avoids a PyYAML dependency for a handful of scalars, one nested mapping and
    two lists.

    Args:
        text: File contents.

    Returns:
        The parsed mapping.
    """
    root: dict[str, Any] = {}
    current_key: str | None = None
    container: Any = None
    container_indent = -1

    for raw in text.splitlines():
        line = raw.split("#")[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if stripped.startswith("- "):
            if isinstance(container, list):
                container.append(_scalar(stripped[2:]))
            continue

        if container is not None and indent > container_indent:
            key, _, value = stripped.partition(":")
            if isinstance(container, dict):
                container[key.strip()] = _scalar(value)
                continue

        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()
        if value:
            root[key] = _scalar(value)
            container, current_key, container_indent = None, None, -1
        else:
            container = [] if key in _LIST_KEYS else {}
            root[key] = container
            current_key, container_indent = key, indent
    return root


_LIST_KEYS = {"gpu_types", "allowed_cuda_versions"}


def _scalar(value: str) -> Any:
    value = value.strip().strip('"')
    if value in {"null", "~", ""}:
        return None
    if value in {"true", "false"}:
        return value == "true"
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def find_by_name(resource: str, name: str, api_key: str) -> str | None:
    """Look up an existing resource id by name.

    Args:
        resource: Either `templates` or `endpoints`.
        name: The name declared in config.
        api_key: RunPod API key.

    Returns:
        The id, or None if it does not exist yet.
    """
    items = _request("GET", f"/{resource}", api_key) or []
    for item in items:
        if item.get("name") == name:
            return str(item["id"])
    return None


def template_body(config: dict[str, Any], tag: str) -> dict[str, Any]:
    """Build the template payload: image and environment.

    Args:
        config: The parsed configuration.
        tag: The immutable image tag to deploy.

    Returns:
        The template create/update body.
    """
    return {
        "name": f"{config['name']}-template",
        "imageName": f"{config['image_repository']}:{tag}",
        "env": dict(config.get("env") or {}),
        "containerDiskInGb": int(config.get("container_disk_gb", 20)),
        "isServerless": True,
    }


def endpoint_body(
    config: dict[str, Any], template_id: str, endpoint_id: str | None
) -> dict[str, Any]:
    """Build the endpoint payload: GPUs, scaling, timeouts, filters.

    `env` and the image are deliberately absent — they belong to the template.
    No network volume and no datacenter pin: weights come from RunPod's model
    cache, so the endpoint is free to run wherever there is capacity.

    Args:
        config: The parsed configuration.
        template_id: The template this endpoint runs.
        endpoint_id: Existing id, when updating.

    Returns:
        The endpoint create/update body.
    """
    workers = config.get("workers") or {}
    body: dict[str, Any] = {
        "name": config["name"],
        "templateId": template_id,
        "computeType": "GPU",
        "gpuCount": 1,
        # Ordered: RunPod rents down the list, so this is automatic fallback
        # when the first choice is scarce.
        "gpuTypeIds": list(config.get("gpu_types") or []),
        "workersMin": int(workers.get("min", 0)),
        "workersMax": int(workers.get("max", 1)),
        "idleTimeout": int(config.get("idle_timeout_s", 5)),
        "executionTimeoutMs": int(config.get("execution_timeout_ms", 600000)),
        "scalerType": str(config.get("scaler_type", "QUEUE_DELAY")),
        "scalerValue": int(config.get("scaler_value", 4)),
        "flashboot": bool(config.get("flashboot", True)),
    }
    # The lever for the CUDA-wheel / host-driver pairing. A cu13x torch wheel
    # needs CUDA-13-capable hosts; declaring it here fails fast on scheduling
    # rather than at model load.
    if config.get("allowed_cuda_versions"):
        body["allowedCudaVersions"] = list(config["allowed_cuda_versions"])
    if endpoint_id:
        body["id"] = endpoint_id
    return body


def apply(config: dict[str, Any], tag: str, api_key: str, dry_run: bool) -> int:
    """Upsert the template, then the endpoint.

    Args:
        config: The parsed configuration.
        tag: The image tag to deploy.
        api_key: RunPod API key.
        dry_run: Print the payloads without calling the API.

    Returns:
        A process exit code.
    """
    tmpl = template_body(config, tag)

    if dry_run:
        print("template:")
        print(json.dumps(tmpl, indent=2))
        print("\nendpoint:")
        print(json.dumps(endpoint_body(config, "<template-id>", None), indent=2))
        print("\ndry run; nothing applied")
        return 0

    template_id = find_by_name("templates", tmpl["name"], api_key)
    if template_id:
        _request("PATCH", f"/templates/{template_id}", api_key, tmpl)
        print(f"template updated  {tmpl['name']}  {template_id}")
    else:
        created = _request("POST", "/templates", api_key, tmpl)
        template_id = str(created["id"])
        print(f"template created  {tmpl['name']}  {template_id}")

    endpoint_id = find_by_name("endpoints", config["name"], api_key)
    body = endpoint_body(config, template_id, endpoint_id)
    if endpoint_id:
        _request("PATCH", f"/endpoints/{endpoint_id}", api_key, body)
        print(f"endpoint updated  {config['name']}  {endpoint_id}")
    else:
        created = _request("POST", "/endpoints", api_key, body)
        endpoint_id = str(created["id"])
        print(f"endpoint created  {config['name']}  {endpoint_id}")

    print(f"\nimage:       {tmpl['imageName']}")
    print(f"endpoint id: {endpoint_id}")
    print("record this tag as the rollback target in docs/RUNBOOK.md")
    return 0


def main() -> int:
    """Parse arguments and apply.

    Returns:
        A process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tag", required=True, help="immutable image tag")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.tag == "latest" or args.tag.endswith(":latest"):
        print("refusing to deploy `latest`; use an immutable tag", file=sys.stderr)
        return 2

    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if not api_key and not args.dry_run:
        print("set RUNPOD_API_KEY", file=sys.stderr)
        return 2

    try:
        return apply(load_config(args.config), args.tag, api_key, args.dry_run)
    except ApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
