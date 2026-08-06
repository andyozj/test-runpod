"""Submit a prompt to the RunPod endpoint and save the image.

Uses RunPod's official Python SDK. Calls the serverless endpoint directly — no
gateway required.

    pip install runpod
    export RUNPOD_API_KEY=... RUNPOD_ENDPOINT_ID=...
    python client/generate.py "a red fox in falling snow" --out fox.png
    python client/generate.py "a red fox" --sync

The SDK wraps the same `/run` and `/status` operations the README shows as raw
`curl`, and handles the polling loop, the `{"input": ...}` envelope and the
retry semantics. Roughly 15 lines of it replace 150 of hand-rolled HTTP.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import runpod
except ImportError:  # pragma: no cover - the message is the whole point
    print("pip install runpod", file=sys.stderr)
    raise SystemExit(2) from None

SYNC_TIMEOUT_S = 300
ASYNC_TIMEOUT_S = 900
POLL_INTERVAL_S = 2


def build_input(args: argparse.Namespace) -> dict[str, Any]:
    """Assemble the job input from parsed arguments.

    Args:
        args: Parsed command-line arguments.

    Returns:
        The `input` payload. Only `prompt` is required; the worker defaults
        everything else and echoes back what it used.
    """
    payload: dict[str, Any] = {
        "prompt": args.prompt,
        "width": args.width,
        "height": args.height,
        "num_inference_steps": args.steps,
        "guidance_scale": args.guidance,
    }
    if args.seed is not None:
        payload["seed"] = args.seed
    return payload


def save(output: dict[str, Any], out: Path, observed_s: float) -> int:
    """Write the image and report what produced it.

    Args:
        output: The handler's return value.
        out: Where to write the image.
        observed_s: Wall clock from submit to image in hand.

    Returns:
        A process exit code.
    """
    if "error" in output:
        error = output["error"]
        if isinstance(error, str):
            # The worker JSON-encodes the envelope because the platform
            # forwards only string errors.
            try:
                error = json.loads(error)
            except ValueError:
                error = {"code": "ERROR", "message": error}
        print(f"{error.get('code', 'ERROR')}: {error.get('message')}", file=sys.stderr)
        if error.get("suggestion"):
            print(f"  {error['suggestion']}", file=sys.stderr)
        return 1

    out.write_bytes(base64.b64decode(output["image_base64"]))
    print(
        f"saved {out}  seed={output['seed']}  "
        f"{output['width']}x{output['height']}  "
        f"inference={output['timings']['inference_s']}s  "
        f"observed={observed_s:.1f}s"
    )
    return 0


def run_sync(endpoint: runpod.Endpoint, payload: dict[str, Any]) -> Any:
    """Submit and wait in one call.

    Args:
        endpoint: The configured endpoint.
        payload: The job input.

    Returns:
        The handler output, or a status dict if the wait expired.
    """
    return endpoint.run_sync(payload, timeout=SYNC_TIMEOUT_S)


def run_async(endpoint: runpod.Endpoint, payload: dict[str, Any]) -> Any:
    """Submit, report status while it runs, then return the output.

    Args:
        endpoint: The configured endpoint.
        payload: The job input.

    Returns:
        The handler output.
    """
    job = endpoint.run(payload)
    print(f"job {job.job_id}", file=sys.stderr)

    last = ""
    deadline = time.monotonic() + ASYNC_TIMEOUT_S
    while (status := job.status()) not in {
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
    }:
        if time.monotonic() >= deadline:
            print(f"  gave up after {ASYNC_TIMEOUT_S}s ({status})", file=sys.stderr)
            return None
        if status != last:
            print(f"  {status}", file=sys.stderr)
            last = status
        time.sleep(POLL_INTERVAL_S)

    print(f"  {status}", file=sys.stderr)
    if status != "COMPLETED":
        # output() is None for failed jobs; the structured envelope rides the
        # record's error field. Same private accessor the e2e suite uses —
        # the SDK exposes no public one.
        record = job._fetch_job()  # noqa: SLF001
        return {"error": record.get("error", status)}
    return job.output()


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
    parser.add_argument(
        "--sync",
        action="store_true",
        help="use /runsync — one call, but a cold worker will outlast it",
    )
    args = parser.parse_args()

    runpod.api_key = os.environ.get("RUNPOD_API_KEY", "")
    endpoint_id = os.environ.get("RUNPOD_ENDPOINT_ID", "")
    if not runpod.api_key or not endpoint_id:
        print("set RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID", file=sys.stderr)
        return 2

    endpoint = runpod.Endpoint(endpoint_id)
    payload = build_input(args)

    started = time.monotonic()
    output = run_sync(endpoint, payload) if args.sync else run_async(endpoint, payload)
    observed_s = time.monotonic() - started

    if isinstance(output, dict) and ("error" in output or "image_base64" in output):
        return save(output, args.out, observed_s)
    # run_sync returns a status dict rather than the output when the wait
    # expires — a cold worker loading 33GB will do exactly that.
    print(f"no image returned: {output}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
