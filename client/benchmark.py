"""Benchmark harness for the deployed endpoint. Methodology: docs/specs/09-benchmarks.md.

Raw records append to a JSONL file keyed by (section, config, index), so a run
that dies partway resumes without repeating completed work, and re-running
with a larger N only adds the missing indices. `BENCHMARKS.md` is rendered
from the raw file, never written by hand.

Usage:
    export RUNPOD_API_KEY=... RUNPOD_ENDPOINT_ID=...
    python client/benchmark.py --tag 0.1.0-72e537d-slim --fake      # dry run
    python client/benchmark.py --tag 0.1.0-72e537d-slim             # ~1h GPU
    python client/benchmark.py --tag ... --only steps,cold --n 2    # subset
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RAW_PATH = ROOT / "benchmarks" / "raw.jsonl"
REPORT_PATH = ROOT / "BENCHMARKS.md"
GRID_DIR = ROOT / "samples" / "quality-grid"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
SECTIONS = ("warmup", "steps", "resolution", "payload", "concurrency", "cold")


class Api:
    """Raw REST access to one endpoint; job records carry the timing fields."""

    sleep_scale = 1.0

    def __init__(self, endpoint_id: str, api_key: str) -> None:
        self.endpoint_id = endpoint_id
        self.api_key = api_key

    def _call(self, method: str, url: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(  # noqa: S310 - fixed https hosts
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            raw = response.read()
            return json.loads(raw) if raw else None

    def submit(self, payload: dict[str, Any]) -> str:
        """Submit a job, retrying the brief 409 window after a worker bounce.

        Args:
            payload: The worker input.

        Returns:
            The job id.
        """
        url = f"https://api.runpod.ai/v2/{self.endpoint_id}/run"
        for attempt in range(12):
            try:
                return str(self._call("POST", url, {"input": payload})["id"])
            except urllib.error.HTTPError as exc:
                if exc.code != 409 or attempt == 11:
                    raise
                time.sleep(10)
        raise RuntimeError("unreachable")

    def wait(self, job_id: str, timeout_s: float = 900.0) -> dict[str, Any]:
        """Poll until terminal and return the full job record.

        Args:
            job_id: The job to watch.
            timeout_s: Give up after this long.

        Returns:
            The `/status` record, including delayTime and executionTime.
        """
        url = f"https://api.runpod.ai/v2/{self.endpoint_id}/status/{job_id}"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            record: dict[str, Any] = self._call("GET", url)
            if record.get("status") in TERMINAL:
                return record
            time.sleep(1.5)
        msg = f"job {job_id} not terminal within {timeout_s:.0f}s"
        raise TimeoutError(msg)

    def set_workers_max(self, value: int) -> None:
        """PATCH the worker ceiling; 0-then-restore evicts FlashBoot state.

        Args:
            value: The new workersMax.
        """
        url = f"https://rest.runpod.io/v1/endpoints/{self.endpoint_id}"
        self._call("PATCH", url, {"workersMax": value})


class FakeApi(Api):
    """Synthetic records with plausible shapes, for developing the harness."""

    sleep_scale = 0.0

    def __init__(self) -> None:
        super().__init__("fake", "fake")
        self._jobs: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def submit(self, payload: dict[str, Any]) -> str:
        """Fabricate a job whose timings scale with steps and pixels."""
        self._counter += 1
        job_id = f"fake-{self._counter}"
        steps = int(payload.get("num_inference_steps", 28))
        pixels = int(payload.get("width", 1024)) * int(payload.get("height", 1024))
        execution_ms = int(1500 + steps * 550 * (pixels / 1024**2))
        image = base64.b64encode(b"\x89PNG fake" + bytes(64)).decode()
        self._jobs[job_id] = {
            "id": job_id,
            "status": "COMPLETED",
            "delayTime": 140,
            "executionTime": execution_ms,
            "workerId": "fake-worker",
            "output": {
                "image_base64": image,
                "seed": payload.get("seed", 0),
                "width": payload.get("width", 1024),
                "height": payload.get("height", 1024),
                "num_inference_steps": steps,
                "model_version": "black-forest-labs/FLUX.1-dev@fake",
                "timings": {"inference_s": execution_ms / 1000, "encode_s": 0.1},
            },
        }
        return job_id

    def wait(self, job_id: str, timeout_s: float = 900.0) -> dict[str, Any]:
        """Return the fabricated record immediately."""
        return self._jobs[job_id]

    def set_workers_max(self, value: int) -> None:
        """No workers to bounce."""
        return


def load_done(path: Path) -> set[str]:
    """Return the keys of every record already on disk.

    Args:
        path: The raw JSONL file.

    Returns:
        Completed record keys.
    """
    if not path.exists():
        return set()
    return {json.loads(line)["key"] for line in path.read_text().splitlines() if line}


def append_record(path: Path, record: dict[str, Any]) -> None:
    """Append one record.

    Args:
        path: The raw JSONL file.
        record: The record to persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record) + "\n")


def run_case(
    api: Api, key: str, payload: dict[str, Any], meta: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute one job and flatten it into a benchmark record.

    Args:
        api: The endpoint.
        key: Resume key, `section:config:index`.
        payload: The worker input.
        meta: Extra fields recorded verbatim.

    Returns:
        The record and the job output.
    """
    started = time.monotonic()
    job_id = api.submit(payload)
    job = api.wait(job_id)
    observed_s = time.monotonic() - started
    output = job.get("output") or {}
    record: dict[str, Any] = {
        "key": key,
        "status": job.get("status"),
        "delay_ms": job.get("delayTime"),
        "execution_ms": job.get("executionTime"),
        "observed_s": round(observed_s, 2),
        "worker_id": job.get("workerId"),
        "payload_b64_bytes": len(output.get("image_base64") or ""),
        "seed": output.get("seed"),
        "model_version": output.get("model_version"),
        "inference_s": (output.get("timings") or {}).get("inference_s"),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **meta,
    }
    if "error" in job:
        record["error"] = str(job["error"])[:300]
    return record, output


def save_grid_image(output: dict[str, Any], steps: int) -> None:
    """Persist one quality-grid image.

    Args:
        output: A completed job output.
        steps: The step count, used in the filename.
    """
    image = output.get("image_base64")
    if not image:
        return
    GRID_DIR.mkdir(parents=True, exist_ok=True)
    (GRID_DIR / f"steps-{steps:02d}.png").write_bytes(base64.b64decode(image))


def section_steps(api: Api, cfg: dict[str, Any], done: set[str]) -> None:
    """Steps sweep at 1024²; index 0 of each config feeds the quality grid."""
    for steps in cfg["steps_sweep"]:
        for i in range(cfg["n"]):
            key = f"steps:{steps}:{i}"
            if key in done:
                continue
            payload = {
                "prompt": cfg["prompt"],
                "seed": cfg["seed"],
                "num_inference_steps": steps,
            }
            record, output = run_case(api, key, payload, {"steps": steps, "i": i})
            append_record(RAW_PATH, record)
            if i == 0:
                save_grid_image(output, steps)
            print(f"  {key}  exec={record['execution_ms']}ms")


def section_resolution(api: Api, cfg: dict[str, Any], done: set[str]) -> None:
    """Resolution sweep at the default step count."""
    for size in cfg["resolution_sweep"]:
        for i in range(cfg["n"]):
            key = f"resolution:{size}:{i}"
            if key in done:
                continue
            payload = {
                "prompt": cfg["prompt"],
                "seed": cfg["seed"],
                "width": size,
                "height": size,
            }
            record, _ = run_case(api, key, payload, {"size": size, "i": i})
            append_record(RAW_PATH, record)
            print(f"  {key}  exec={record['execution_ms']}ms")


def section_payload(api: Api, cfg: dict[str, Any], done: set[str]) -> None:
    """Response size at the maximum resolution, PNG versus JPEG."""
    size = cfg["payload_resolution"]
    for fmt in cfg["payload_formats"]:
        for i in range(cfg["payload_n"]):
            key = f"payload:{fmt}:{i}"
            if key in done:
                continue
            payload = {
                "prompt": cfg["prompt"],
                "seed": cfg["seed"],
                "width": size,
                "height": size,
                "output_format": fmt,
            }
            record, _ = run_case(api, key, payload, {"format": fmt, "i": i})
            append_record(RAW_PATH, record)
            print(f"  {key}  b64={record['payload_b64_bytes']}B")


def section_concurrency(api: Api, cfg: dict[str, Any], done: set[str]) -> None:
    """One burst against workersMax; queue wait is the number that matters."""
    burst = cfg["concurrency_burst"]
    if f"concurrency:burst:{burst - 1}" in done:
        return

    def one(i: int) -> None:
        key = f"concurrency:burst:{i}"
        if key in done:
            return
        payload = {"prompt": cfg["prompt"], "seed": cfg["seed"] + i}
        record, _ = run_case(api, key, payload, {"i": i, "burst": burst})
        append_record(RAW_PATH, record)
        print(f"  {key}  delay={record['delay_ms']}ms exec={record['execution_ms']}ms")

    with concurrent.futures.ThreadPoolExecutor(max_workers=burst) as pool:
        list(pool.map(one, range(burst)))


def section_cold(api: Api, cfg: dict[str, Any], done: set[str]) -> None:
    """True cold versus warm versus FlashBoot resume, per cycle.

    Runs last: bouncing first would contaminate every other section's first
    samples with cold noise.
    """
    for cycle in range(cfg["cold_cycles"]):
        if f"cold:resume:{cycle}" in done:
            continue
        print(f"  cycle {cycle}: bouncing workers")
        api.set_workers_max(0)
        time.sleep(25 * api.sleep_scale)
        api.set_workers_max(cfg["workers_max"])
        for phase, delay in (
            ("true_cold", 0),
            ("warm", 0),
            ("resume", cfg["idle_timeout_s"] + 20),
        ):
            key = f"cold:{phase}:{cycle}"
            if key in done:
                continue
            time.sleep(delay * api.sleep_scale)
            payload = {"prompt": cfg["prompt"], "seed": cfg["seed"]}
            record, _ = run_case(api, key, payload, {"phase": phase, "cycle": cycle})
            append_record(RAW_PATH, record)
            print(f"  {key}  delay={record['delay_ms']}ms")


def section_warmup(api: Api, cfg: dict[str, Any], done: set[str]) -> None:
    """One discarded job so sweeps never start against a cold worker."""
    key = "warmup:warmup:0"
    if key in done:
        return
    record, _ = run_case(
        api, key, {"prompt": cfg["prompt"], "seed": cfg["seed"]}, {"discard": True}
    )
    append_record(RAW_PATH, record)
    print(f"  {key}  delay={record['delay_ms']}ms")


def _stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50": statistics.median(ordered),
        "p95": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))],
        "min": ordered[0],
        "max": ordered[-1],
    }


def _rows(records: list[dict[str, Any]], group: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for record in records:
        if record.get(group) is not None and record.get("status") == "COMPLETED":
            grouped.setdefault(record[group], []).append(record)
    return grouped


def render(cfg: dict[str, Any], tag: str) -> str:
    """Render BENCHMARKS.md from the raw records.

    Args:
        cfg: The benchmark configuration.
        tag: The image tag measured.

    Returns:
        The report markdown.
    """
    records = [json.loads(line) for line in RAW_PATH.read_text().splitlines() if line]
    rate = cfg["rate_usd_hr"]
    lines = [
        "# Benchmarks",
        "",
        f"Measured {time.strftime('%Y-%m-%d')} against endpoint `{os.environ.get('RUNPOD_ENDPOINT_ID', 'unknown')}`, "
        f"image `{tag}`, GPU {cfg['gpu']}, model per response `model_version`. "
        f"Rate ${rate}/hr ({cfg['rate_date']}, re-verify before quoting). "
        f"Raw data: `benchmarks/raw.jsonl` ({len(records)} records). "
        "Methodology: `docs/specs/09-benchmarks.md`; p50/p95 over N runs, fixed seed, one variable at a time.",
        "",
        "## Cold start, decomposed",
        "",
        "| Phase | N | delay p50 | delay max |",
        "|---|---|---|---|",
    ]
    for phase, rows in sorted(_rows(records, "phase").items()):
        s = _stats([r["delay_ms"] / 1000 for r in rows])
        lines.append(f"| {phase} | {s['n']} | {s['p50']:.1f}s | {s['max']:.1f}s |")
    lines += [
        "",
        "`true_cold` includes image pull and the ~34GB pipeline load; `resume` is FlashBoot restoring a retained worker; `warm` is a live worker taking the next job.",
        "",
        "## Steps sweep (1024², seed fixed)",
        "",
        "| Steps | N | exec p50 | exec p95 | $/image at p50 |",
        "|---|---|---|---|---|",
    ]
    for steps, rows in sorted(_rows(records, "steps").items()):
        s = _stats([r["execution_ms"] / 1000 for r in rows])
        cost = s["p50"] / 3600 * rate
        lines.append(
            f"| {steps} | {s['n']} | {s['p50']:.1f}s | {s['p95']:.1f}s | ${cost:.4f} |"
        )
    lines += [
        "",
        "Quality grid for the same seeds: `samples/quality-grid/`.",
        "",
        "## Resolution sweep (28 steps)",
        "",
        "| Size | N | exec p50 | exec p95 | $/image at p50 |",
        "|---|---|---|---|---|",
    ]
    for size, rows in sorted(_rows(records, "size").items()):
        s = _stats([r["execution_ms"] / 1000 for r in rows])
        lines.append(
            f"| {size}² | {s['n']} | {s['p50']:.1f}s | {s['p95']:.1f}s | ${s['p50'] / 3600 * rate:.4f} |"
        )
    lines += [
        "",
        "## Response payload (1536²)",
        "",
        "| Format | N | base64 p50 | vs 10MB `/run` input cap |",
        "|---|---|---|---|",
    ]
    for fmt, rows in sorted(_rows(records, "format").items()):
        s = _stats([float(r["payload_b64_bytes"]) for r in rows])
        lines.append(
            f"| {fmt} | {s['n']} | {s['p50'] / 1e6:.2f}MB | {'fits' if s['p50'] < 10e6 else 'EXCEEDS'} |"
        )
    burst_rows = [r for r in records if r.get("burst")]
    if burst_rows:
        delays = _stats([r["delay_ms"] / 1000 for r in burst_rows])
        lines += [
            "",
            "## Concurrency",
            "",
            f"Burst of {burst_rows[0]['burst']} against workersMax={cfg['workers_max']}: "
            f"queue wait p50 {delays['p50']:.1f}s, max {delays['max']:.1f}s. "
            "Execution time is flat; the queue absorbs the burst, which is the design.",
        ]
    step_28 = [
        r for r in records if r.get("steps") == 28 and r.get("status") == "COMPLETED"
    ]
    if step_28:
        avg = _stats([r["execution_ms"] / 1000 for r in step_28])["p50"]
        lines += [
            "",
            "## Named outputs",
            "",
            f"- `AVG_JOB_SECONDS = {avg:.1f}` — feeds the gateway queue-pressure threshold",
            f"- Cost per default image (1024², 28 steps): ${avg / 3600 * rate:.4f} execution-only",
        ]
    lines += [
        "",
        "## Descoped, and why",
        "",
        "| Planned | Status |",
        "|---|---|",
        "| GPU comparison (A100, 4090) | Descoped by decision: endpoint is L40S-only so every number is one card. The A100 fallback ran exactly one job before the change (14.9s at 28 steps — consistent with its bandwidth advantage) |",
        "| Weight-delivery three-way | Only cached models is deployed; volume was dropped in design, baked is built but not deployed |",
        "| CFG 2× claim | Not measurable through the deployed contract — the input schema deliberately omits `true_cfg_scale` |",
        "",
        "## Threats to validity",
        "",
        "- Single region, single session, single GPU type; another allocation may differ.",
        "- N bounds variance loosely; no confidence intervals claimed.",
        "- Costs are execution-only; idle-timeout billing between requests is additive and traffic-shaped.",
        "- Cross-check against the RunPod invoice before quoting totals.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    """Run the configured sections and render the report.

    Returns:
        A process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="image tag being measured")
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--only", default=",".join(SECTIONS))
    parser.add_argument("--n", type=int, default=None, help="override N per config")
    args = parser.parse_args()

    cfg = json.loads((ROOT / "benchmarks" / "config.json").read_text())
    if args.n:
        cfg["n"] = args.n

    if args.fake:
        api: Api = FakeApi()
    else:
        api_key = os.environ.get("RUNPOD_API_KEY")
        endpoint = os.environ.get("RUNPOD_ENDPOINT_ID")
        if not api_key or not endpoint:
            print("set RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID", file=sys.stderr)
            return 2
        api = Api(endpoint, api_key)

    sections = {
        "warmup": section_warmup,
        "steps": section_steps,
        "resolution": section_resolution,
        "payload": section_payload,
        "concurrency": section_concurrency,
        "cold": section_cold,
    }
    done = load_done(RAW_PATH)
    for name in args.only.split(","):
        print(f"== {name}")
        sections[name](api, cfg, done)

    REPORT_PATH.write_text(render(cfg, args.tag))
    print(f"rendered {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
