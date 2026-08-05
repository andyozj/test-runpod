"""Create or update a RunPod serverless endpoint from committed config.

RunPod's GraphQL `saveEndpoint` mutation both creates and updates — passing an
existing id modifies in place. So the endpoints are declared in
`deploy/endpoints/*.yaml` and applied from here rather than clicked together in
a console, which makes the configuration reviewable in a diff and
reconstructible after an accidental deletion.

Usage:
    export RUNPOD_API_KEY=...
    python scripts/apply_endpoint.py --config deploy/endpoints/baked.yaml \\
        --tag 0.1.0-a3f21c8-baked
    python scripts/apply_endpoint.py --config deploy/endpoints/baked.yaml \\
        --tag 0.1.0-a3f21c8-baked --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

GRAPHQL_URL = "https://api.runpod.io/graphql"

SAVE_ENDPOINT = """
mutation saveEndpoint($input: EndpointInput!) {
  saveEndpoint(input: $input) {
    id
    name
    templateId
    gpuIds
    workersMin
    workersMax
    idleTimeout
    executionTimeoutMs
  }
}
"""

LIST_ENDPOINTS = """
query Endpoints {
  myself {
    endpoints { id name }
  }
}
"""


def load_config(path: Path) -> dict[str, Any]:
    """Read an endpoint declaration.

    Uses a minimal parser rather than adding a YAML dependency for a handful of
    flat keys.

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

    Args:
        text: File contents.

    Returns:
        The parsed mapping.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    pending_list: list[str] | None = None

    for raw in text.splitlines():
        line = raw.split("#")[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if stripped.startswith("- "):
            if pending_list is not None:
                pending_list.append(_scalar(stripped[2:]))
            continue

        pending_list = None
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            child: dict[str, Any] | list[str] = []
            parent[key] = child
            pending_list = child  # type: ignore[assignment]
            nested: dict[str, Any] = {}
            stack.append((indent, nested))
            parent[key] = nested if not pending_list else child
        else:
            parent[key] = _scalar(value)
    return _collapse(root)


def _collapse(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _collapse(v) for k, v in node.items() if v != {} or True}
    return node


def _scalar(value: str) -> Any:
    value = value.strip().strip('"')
    if value in {"null", "~", ""}:
        return None
    if value in {"true", "false"}:
        return value == "true"
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def _graphql(query: str, variables: dict[str, Any], api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - fixed https host
        f"{GRAPHQL_URL}?api_key={api_key}",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        body: dict[str, Any] = json.load(response)
    if "errors" in body:
        raise RuntimeError(json.dumps(body["errors"], indent=2))
    return body["data"]


def find_endpoint_id(name: str, api_key: str) -> str | None:
    """Look up an existing endpoint by name.

    Args:
        name: The endpoint name from the config.
        api_key: RunPod API key.

    Returns:
        The endpoint id, or None if it does not exist yet.
    """
    data = _graphql(LIST_ENDPOINTS, {}, api_key)
    for endpoint in data["myself"]["endpoints"]:
        if endpoint["name"] == name:
            return str(endpoint["id"])
    return None


def build_input(
    config: dict[str, Any], tag: str, endpoint_id: str | None
) -> dict[str, Any]:
    """Translate the declaration into the mutation input.

    Args:
        config: The parsed configuration.
        tag: The image tag to deploy.
        endpoint_id: Existing id, so the mutation updates in place.

    Returns:
        The `EndpointInput` payload.
    """
    workers = config.get("workers", {})
    payload: dict[str, Any] = {
        "name": config["name"],
        "gpuIds": ",".join(config.get("gpu_types", [])),
        "workersMin": workers.get("min", 0),
        "workersMax": workers.get("max", 1),
        "idleTimeout": config.get("idle_timeout_s", 5),
        "executionTimeoutMs": config.get("execution_timeout_ms", 600000),
        "flashboot": config.get("flashboot", True),
        "imageName": f"{config['image_repository']}:{tag}",
        "env": [
            {"key": key, "value": str(value)}
            for key, value in (config.get("env") or {}).items()
        ],
    }
    if config.get("network_volume_id"):
        payload["networkVolumeId"] = config["network_volume_id"]
    if config.get("datacenter"):
        payload["locations"] = config["datacenter"]
    if endpoint_id:
        payload["id"] = endpoint_id
    return payload


def main() -> int:
    """Apply one endpoint declaration.

    Returns:
        A process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tag", required=True, help="immutable image tag")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.tag.endswith(":latest") or args.tag == "latest":
        print("refusing to deploy `latest`; use an immutable tag", file=sys.stderr)
        return 2

    config = load_config(args.config)
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if not api_key and not args.dry_run:
        print("set RUNPOD_API_KEY", file=sys.stderr)
        return 2

    endpoint_id = None if args.dry_run else find_endpoint_id(config["name"], api_key)
    payload = build_input(config, args.tag, endpoint_id)

    action = "update" if endpoint_id else "create"
    print(f"{action} {config['name']} -> {payload['imageName']}")
    print(json.dumps(payload, indent=2))

    if args.dry_run:
        print("dry run; nothing applied")
        return 0

    result = _graphql(SAVE_ENDPOINT, {"input": payload}, api_key)
    endpoint = result["saveEndpoint"]
    print(f"applied. endpoint id: {endpoint['id']}")
    print("record this tag as the rollback target in docs/RUNBOOK.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
