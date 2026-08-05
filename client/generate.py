"""Submit a prompt to the RunPod endpoint and save the image.

Calls the serverless endpoint directly — no gateway required. RunPod's job
surface is fixed (`/run`, `/status/{id}`), so this is submit-then-poll.

Usage:
    export RUNPOD_API_KEY=... RUNPOD_ENDPOINT_ID=...
    python client/generate.py "a red fox in falling snow" --out fox.png
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE = "https://api.runpod.ai/v2"
POLL_INTERVAL_S = 2.0
POLL_BACKOFF_AFTER_S = 60.0
POLL_BACKOFF_S = 5.0
DEADLINE_S = 600.0
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}


def _post(url: str, api_key: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - fixed https host
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def _get(url: str, api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - fixed https host
        url, headers={"Authorization": f"Bearer {api_key}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def submit(endpoint: str, api_key: str, payload: dict[str, Any]) -> str:
    """Submit a job and return its id.

    Args:
        endpoint: RunPod endpoint id.
        api_key: RunPod API key.
        payload: The generation parameters.

    Returns:
        The RunPod job id.
    """
    response = _post(f"{BASE}/{endpoint}/run", api_key, {"input": payload})
    return str(response["id"])


def poll(endpoint: str, api_key: str, job_id: str) -> dict[str, Any]:
    """Poll until the job reaches a terminal state.

    Args:
        endpoint: RunPod endpoint id.
        api_key: RunPod API key.
        job_id: The job to watch.

    Returns:
        The final status payload.

    Raises:
        TimeoutError: The job did not finish within the deadline.
    """
    started = time.monotonic()
    while True:
        elapsed = time.monotonic() - started
        if elapsed > DEADLINE_S:
            msg = f"job {job_id} did not finish within {DEADLINE_S:.0f}s"
            raise TimeoutError(msg)

        status = _get(f"{BASE}/{endpoint}/status/{job_id}", api_key)
        state = status.get("status", "UNKNOWN")

        progress = status.get("output") if state == "IN_PROGRESS" else None
        if isinstance(progress, dict) and "percent" in progress:
            print(f"\r  {state} {progress['percent']}%", end="", file=sys.stderr)
        else:
            print(f"\r  {state} {elapsed:.0f}s", end="", file=sys.stderr)

        if state in TERMINAL:
            print(file=sys.stderr)
            return status
        time.sleep(
            POLL_BACKOFF_S if elapsed > POLL_BACKOFF_AFTER_S else POLL_INTERVAL_S
        )


def main() -> int:
    """Run one generation end to end.

    Returns:
        A process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--out", type=Path, default=Path("output.png"))
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--steps", type=int, default=28)
    parser.add_argument("--guidance", type=float, default=3.5)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()

    api_key = os.environ.get("RUNPOD_API_KEY")
    endpoint = os.environ.get("RUNPOD_ENDPOINT_ID")
    if not api_key or not endpoint:
        print("set RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID", file=sys.stderr)
        return 2

    payload: dict[str, Any] = {
        "prompt": args.prompt,
        "width": args.width,
        "height": args.height,
        "num_inference_steps": args.steps,
        "guidance_scale": args.guidance,
    }
    if args.seed is not None:
        payload["seed"] = args.seed

    wall_started = time.monotonic()
    try:
        job_id = submit(endpoint, api_key, payload)
    except urllib.error.HTTPError as exc:
        print(f"submit failed: {exc.code} {exc.reason}", file=sys.stderr)
        return 1

    print(f"job {job_id}", file=sys.stderr)
    status = poll(endpoint, api_key, job_id)
    observed_s = time.monotonic() - wall_started

    output = status.get("output") or {}
    if status.get("status") != "COMPLETED" or "error" in output:
        print(json.dumps(status, indent=2), file=sys.stderr)
        return 1

    args.out.write_bytes(base64.b64decode(output["image_base64"]))
    print(
        f"saved {args.out}  seed={output['seed']}  "
        f"{output['width']}x{output['height']}  "
        f"inference={output['timings']['inference_s']}s  "
        f"observed={observed_s:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
