r"""Create or update the stack pod from committed config.

Targets the REST API v2 at `https://api.runpod.io/v2` (public beta
2026-07-23), not the v1 surface `apply_endpoint.py` uses: pods are a
first-class v2 resource and v1 models them differently.

One resource, one upsert: look the pod up by name, create if absent, patch if
present. The v2 schema worth knowing before reading the code:

    POST  /pods         image, cpu{id,vcpuCount}, cloud, disk, ports, env,
                        mounts{persistent{size,path}}
    PATCH /pods/{id}    mutable subset only — cpu and cloud are create-time
    POST  /pods/{id}/action   {"action": "start|stop|restart|terminate"}

A patched image takes effect on the next start, so after an update the script
restarts a running pod (or starts an exited one) unless told not to.

Secrets never live in the YAML: keys listed under `secret_env` are read from
the caller's environment at apply time and injected into the pod env.

Usage:
    export RUNPOD_API_KEY=... RUNPOD_ENDPOINT_ID=... GATEWAY_API_KEYS=...
    python scripts/apply_pod.py --config deploy/pods/stack.yaml \\
        --tag 0.1.0-a3f21c8 --dry-run
    python scripts/apply_pod.py --config deploy/pods/stack.yaml \\
        --tag 0.1.0-a3f21c8
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

# Everything else on the pod is fixed at creation (cpu, cloud, data center).
PATCHABLE_FIELDS = ("name", "image", "disk", "ports", "env", "mounts")


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
            # Cloudflare fronts api.runpod.io and answers the default
            # `Python-urllib/x.y` agent with 403 error 1010.
            "User-Agent": "flux-stack-deploy/1.0",
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
    """Read a pod declaration.

    PyYAML is required rather than falling back to a hand-rolled subset
    parser, for the same reason as `apply_endpoint.py`: a deploy tool must
    fail loudly or not at all. Run via
    `uv run --with pyyaml scripts/apply_pod.py ...` if it is not installed.

    Args:
        path: The YAML file to read.

    Returns:
        The parsed configuration.
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment-dependent
        msg = "PyYAML is required: uv run --with pyyaml scripts/apply_pod.py"
        raise SystemExit(msg) from exc

    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


def find_pod(name: str, api_key: str) -> dict[str, Any] | None:
    """Look up an existing pod by name.

    Args:
        name: The name declared in config.
        api_key: RunPod API key.

    Returns:
        The pod object (id, status, ...), or None if it does not exist yet.
    """
    data = _request("GET", "/pods", api_key) or {}
    pods = data.get("pods", []) if isinstance(data, dict) else data
    for pod in pods:
        if pod.get("name") == name:
            return dict(pod)
    return None


def resolve_secret_env(config: dict[str, Any], dry_run: bool) -> dict[str, str]:
    """Read the `secret_env` names from the caller's environment.

    Args:
        config: The parsed configuration.
        dry_run: Substitute printable placeholders so a dry run never needs —
            and never prints — a real secret.

    Returns:
        Name-to-value mapping to merge into the pod env.

    Raises:
        SystemExit: A declared secret is missing from the environment.
    """
    names = list(config.get("secret_env") or [])
    if dry_run:
        return {name: f"<env:{name}>" for name in names}
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        msg = f"set {', '.join(missing)} (declared under secret_env)"
        raise SystemExit(msg)
    return {name: os.environ[name] for name in names}


def pod_body(
    config: dict[str, Any], tag: str, secrets: dict[str, str]
) -> dict[str, Any]:
    """Build the pod create payload.

    Args:
        config: The parsed configuration.
        tag: The immutable image tag to deploy.
        secrets: Apply-time env values merged over the declared env.

    Returns:
        The pod create body, v2 field names.
    """
    cpu = config.get("cpu") or {}
    return {
        "name": config["name"],
        "image": f"{config['image_repository']}:{tag}",
        "cloud": str(config.get("cloud", "SECURE")),
        "cpu": {
            "id": str(cpu.get("flavor", "cpu3g")),
            "vcpuCount": int(cpu.get("vcpu_count", 2)),
        },
        "disk": int(config.get("container_disk_gb", 20)),
        "ports": [str(p) for p in config.get("ports") or []],
        "env": {**dict(config.get("env") or {}), **secrets},
        "mounts": {
            "persistent": {
                "size": int(config.get("volume_gb", 20)),
                "path": str(config.get("volume_mount_path", "/workspace")),
            }
        },
    }


def patch_body(body: dict[str, Any]) -> dict[str, Any]:
    """Reduce a create payload to the fields PATCH accepts.

    Args:
        body: A full create payload.

    Returns:
        The mutable subset — cpu and cloud are create-time only.
    """
    return {k: body[k] for k in PATCHABLE_FIELDS}


def _restart(pod: dict[str, Any], api_key: str) -> None:
    """Bring the pod up on the just-patched spec.

    A patched image does not take effect on a running pod until its next
    start, so skipping this leaves the previous release serving indefinitely —
    the pod cousin of the FlashBoot staleness `apply_endpoint.py` bounces.

    Args:
        pod: The pod object from the pre-patch lookup.
        api_key: RunPod API key.
    """
    status = str(pod.get("status", ""))
    action = {"RUNNING": "restart", "EXITED": "start", "ERROR": "start"}.get(status)
    if action is None:
        print(f"pod status {status or 'unknown'}: no restart issued")
        return
    _request("POST", f"/pods/{pod['id']}/action", api_key, {"action": action})
    print(f"pod {action}ed      new image takes effect on this boot")


def apply(
    config: dict[str, Any], tag: str, api_key: str, dry_run: bool, restart: bool
) -> int:
    """Upsert the pod.

    Args:
        config: The parsed configuration.
        tag: The image tag to deploy.
        api_key: RunPod API key.
        dry_run: Print the payload without calling the API.
        restart: After an update, restart onto the new image.

    Returns:
        A process exit code.
    """
    secrets = resolve_secret_env(config, dry_run)
    body = pod_body(config, tag, secrets)

    if dry_run:
        print("pod:")
        print(json.dumps(body, indent=2))
        print("\ndry run; nothing applied")
        return 0

    pod = find_pod(config["name"], api_key)
    if pod:
        pod_id = str(pod["id"])
        _request("PATCH", f"/pods/{pod_id}", api_key, patch_body(body))
        print(f"pod updated       {config['name']}  {pod_id}")
        if restart:
            _restart(pod, api_key)
    else:
        created = _request("POST", "/pods", api_key, body)
        pod_id = str(created["id"])
        print(f"pod created       {config['name']}  {pod_id}")

    print(f"\nimage:  {body['image']}")
    print(f"pod id: {pod_id}")
    print(f"url:    https://{pod_id}-8000.proxy.runpod.net")
    print("record this tag as the rollback target in docs/RUNBOOK.md")
    return 0


def is_latest_tag(tag: str) -> bool:
    """Return whether a tag is (or resolves to) the mutable `latest` tag.

    Args:
        tag: The requested image tag.

    Returns:
        True if applying this tag would mean deploying `latest`.

    Example:
        >>> is_latest_tag("0.1.0-a3f21c8")
        False
        >>> is_latest_tag("latest")
        True
        >>> is_latest_tag("ghcr.io/owner/flux-stack:latest")
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
        "--no-restart",
        action="store_true",
        help="skip the restart after an update (the running pod keeps serving the old image)",
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
            restart=not args.no_restart,
        )
    except ApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
