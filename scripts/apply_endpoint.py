r"""Create or update a RunPod serverless endpoint from committed config.

Targets the REST API v2 at `https://api.runpod.io/v2` (public beta
2026-07-23), same surface as `apply_pod.py`. v1 split a deploy into a template
(image, env, disk) plus an endpoint referencing it; v2 folds both into one
`/serverless` resource, so a deploy is one upsert: look the endpoint up by
name, create if absent, patch if present.

The v2 schema worth knowing before reading the code:

    POST  /serverless        name, image, env, disk, type, gpu{pools,count},
                             workers{min,max,idleTimeout}, scaling, timeout,
                             flashboot
    PATCH /serverless/{id}   mutable subset only — type is create-time
    GET   /serverless        {"endpoints": [...]}

Two v1 levers are gone from v2: GPUs are selected by pool (`ADA_48_PRO` =
L40/L40S/6000 Ada), not by exact model, and there is no `allowedCudaVersions`
filter — a CUDA-wheel / host-driver mismatch now surfaces at model load, not
at scheduling.

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

BASE_URL = "https://api.runpod.io/v2"
TIMEOUT_S = 30

# `type` (QUEUE vs LOAD_BALANCER) is fixed at creation.
PATCHABLE_FIELDS = (
    "name",
    "image",
    "env",
    "disk",
    "gpu",
    "workers",
    "scaling",
    "timeout",
    "flashboot",
)


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

    PyYAML is required rather than falling back to a hand-rolled subset
    parser: a parser that guesses list-vs-mapping from a hardcoded key set
    silently misreads any new key, and a deploy tool must fail loudly or not
    at all. Run via `uv run --with pyyaml scripts/apply_endpoint.py ...` if it
    is not already installed.

    Args:
        path: The YAML file to read.

    Returns:
        The parsed configuration.
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment-dependent
        msg = "PyYAML is required: uv run --with pyyaml scripts/apply_endpoint.py"
        raise SystemExit(msg) from exc

    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


def find_endpoint(name: str, api_key: str) -> dict[str, Any] | None:
    """Look up an existing endpoint by name.

    Args:
        name: The name declared in config.
        api_key: RunPod API key.

    Returns:
        The endpoint object (id, ...), or None if it does not exist yet.
    """
    data = _request("GET", "/serverless", api_key) or {}
    endpoints = data.get("endpoints", []) if isinstance(data, dict) else data
    for endpoint in endpoints:
        if endpoint.get("name") == name:
            return dict(endpoint)
    return None


def endpoint_body(config: dict[str, Any], tag: str) -> dict[str, Any]:
    """Build the endpoint create payload.

    Args:
        config: The parsed configuration.
        tag: The immutable image tag to deploy.

    Returns:
        The endpoint create body, v2 field names.
    """
    workers = config.get("workers") or {}
    scaler_value = int(config.get("scaler_value", 4))
    scaling: dict[str, Any] = (
        {"type": "REQUEST_COUNT", "requestCount": scaler_value}
        if str(config.get("scaler_type", "QUEUE_DELAY")) == "REQUEST_COUNT"
        else {"type": "QUEUE_DELAY", "queueDelay": scaler_value}
    )
    workers_body: dict[str, Any] = {
        "min": int(workers.get("min", 0)),
        "max": int(workers.get("max", 1)),
    }
    # v2 rejects idleTimeout on endpoints scaling on requestCount.
    if scaling["type"] != "REQUEST_COUNT":
        workers_body["idleTimeout"] = int(config.get("idle_timeout_s", 5))
    return {
        "name": config["name"],
        "image": f"{config['image_repository']}:{tag}",
        "env": dict(config.get("env") or {}),
        "disk": int(config.get("container_disk_gb", 20)),
        "type": "QUEUE",
        "gpu": {
            "pools": list(config.get("gpu_pools") or []),
            "count": 1,
        },
        "workers": workers_body,
        "scaling": scaling,
        "timeout": int(config.get("execution_timeout_ms", 600000)),
        "flashboot": "FLASHBOOT" if bool(config.get("flashboot", True)) else "OFF",
    }


def patch_body(body: dict[str, Any]) -> dict[str, Any]:
    """Reduce a create payload to the fields PATCH accepts.

    Args:
        body: A full create payload.

    Returns:
        The mutable subset — type is create-time only.
    """
    return {k: body[k] for k in PATCHABLE_FIELDS}


def _bounce_workers(endpoint_id: str, workers: dict[str, Any], api_key: str) -> None:
    """Force every worker to restart on the new release.

    v2 releases roll out "as workers cycle", but a FlashBoot-retained worker
    is neither idle nor processing, so it never cycles — it keeps serving the
    previous image indefinitely (observed 2026-08-06 on v1; same retention
    mechanism). Dropping workers.max to zero evicts the retained state;
    restoring it lets fresh workers boot the current image. Submissions 409
    briefly during the transition.

    Args:
        endpoint_id: The endpoint to bounce.
        workers: The configured workers object to restore.
        api_key: RunPod API key.
    """
    import time

    down = {**workers, "max": 0}
    _request("PATCH", f"/serverless/{endpoint_id}", api_key, {"workers": down})
    time.sleep(20)
    _request("PATCH", f"/serverless/{endpoint_id}", api_key, {"workers": workers})
    print(f"workers bounced   0 -> {workers['max']}; stale FlashBoot state evicted")


def apply(
    config: dict[str, Any], tag: str, api_key: str, dry_run: bool, bounce: bool
) -> int:
    """Upsert the endpoint.

    Args:
        config: The parsed configuration.
        tag: The image tag to deploy.
        api_key: RunPod API key.
        dry_run: Print the payload without calling the API.
        bounce: After an update, force workers off the previous release.

    Returns:
        A process exit code.
    """
    body = endpoint_body(config, tag)

    if dry_run:
        print("endpoint:")
        print(json.dumps(body, indent=2))
        print("\ndry run; nothing applied")
        return 0

    existing = find_endpoint(config["name"], api_key)
    if existing:
        endpoint_id = str(existing["id"])
        _request("PATCH", f"/serverless/{endpoint_id}", api_key, patch_body(body))
        print(f"endpoint updated  {config['name']}  {endpoint_id}")
        if bounce:
            _bounce_workers(endpoint_id, body["workers"], api_key)
    else:
        created = _request("POST", "/serverless", api_key, body)
        endpoint_id = str(created["id"])
        print(f"endpoint created  {config['name']}  {endpoint_id}")

    print(f"\nimage:       {body['image']}")
    print(f"endpoint id: {endpoint_id}")
    print("record this tag as the rollback target in docs/RUNBOOK.md")
    return 0


def is_latest_tag(tag: str) -> bool:
    """Return whether a tag is (or resolves to) the mutable `latest` tag.

    Args:
        tag: The requested image tag.

    Returns:
        True if applying this tag would mean deploying `latest`.

    Example:
        >>> is_latest_tag("0.1.0-a3f21c8-slim")
        False
        >>> is_latest_tag("latest")
        True
        >>> is_latest_tag("ghcr.io/owner/flux-worker:latest")
        True
    """
    return tag == "latest" or tag.endswith(":latest")


def main() -> int:
    """Parse arguments and apply.

    Returns:
        A process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tag", required=True, help="immutable image tag")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-bounce",
        action="store_true",
        help="skip the worker bounce after an update (stale FlashBoot workers keep serving the old image)",
    )
    args = parser.parse_args()

    if is_latest_tag(args.tag):
        print("refusing to deploy `latest`; use an immutable tag", file=sys.stderr)
        return 2

    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if not api_key and not args.dry_run:
        print("set RUNPOD_API_KEY", file=sys.stderr)
        return 2

    try:
        return apply(
            load_config(args.config),
            args.tag,
            api_key,
            args.dry_run,
            bounce=not args.no_bounce,
        )
    except ApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
