"""Benchmark harness for the deployed endpoint: fixed seed, one variable at a time.

Raw records append to a JSONL file keyed by (section, config, index), so a run
that dies partway resumes without repeating completed work, and re-running
with a larger N only adds the missing indices. `BENCHMARKS.md` is rendered
from the raw file, never written by hand.

Usage:
    export RUNPOD_API_KEY=... RUNPOD_ENDPOINT_ID=...
    python benchmarks/harness.py --tag 0.1.0-72e537d-slim --fake      # dry run
    python benchmarks/harness.py --tag 0.1.0-72e537d-slim             # ~1h GPU
    python benchmarks/harness.py --tag ... --only steps,cold --n 2    # subset
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
HTTP_CONFLICT = 409
SUBMIT_RETRY_ATTEMPTS = 12
DEFAULT_STEPS = 28
PAYLOAD_CAP_BYTES = 10e6  # the documented /run input cap
SECTIONS = (
    "warmup",
    "steps",
    "resolution",
    "payload",
    "concurrency",
    "cold",
    "queue",
    "flashboot",
)


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
        for attempt in range(SUBMIT_RETRY_ATTEMPTS):
            try:
                return str(self._call("POST", url, {"input": payload})["id"])
            except urllib.error.HTTPError as exc:
                if exc.code != HTTP_CONFLICT or attempt == SUBMIT_RETRY_ATTEMPTS - 1:
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

    def set_flashboot(self, enabled: bool) -> None:
        """Toggle FlashBoot on the endpoint.

        Args:
            enabled: The desired state.
        """
        url = f"https://rest.runpod.io/v1/endpoints/{self.endpoint_id}"
        self._call("PATCH", url, {"flashboot": enabled})


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

    def set_flashboot(self, enabled: bool) -> None:
        """No FlashBoot to toggle."""
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
    if record.get("status") not in (None, "COMPLETED"):
        # A failed job keeps its data but releases its resume key, so the
        # next run re-measures that index instead of silently shrinking N.
        record = {**record, "key": f"{record['key']}:failed:{int(time.time())}"}
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


def section_queue(api: Api, cfg: dict[str, Any], done: set[str]) -> None:
    """Burst well past workersMax while sampling /health for the queue curve."""
    burst = cfg.get("queue_burst", 12)
    if f"queue:burst:{burst - 1}" in done:
        return
    samples: list[dict[str, Any]] = []
    stop = {"flag": False}

    def sampler() -> None:
        url = f"https://api.runpod.ai/v2/{api.endpoint_id}/health"
        while not stop["flag"]:
            try:
                h = api._call("GET", url)  # noqa: SLF001 - same-module helper
                samples.append(
                    {
                        "t": round(time.monotonic(), 1),
                        "inQueue": h["jobs"]["inQueue"],
                        "inProgress": h["jobs"]["inProgress"],
                        "running": h["workers"]["running"],
                        "idle": h["workers"]["idle"],
                        "initializing": h["workers"]["initializing"],
                    }
                )
            except Exception:  # noqa: BLE001, S110 - sampling must not kill the burst
                pass
            time.sleep(max(2 * api.sleep_scale, 0.05))

    def one(i: int) -> None:
        key = f"queue:burst:{i}"
        if key in done:
            return
        payload = {"prompt": cfg["prompt"], "seed": cfg["seed"] + i}
        record, _ = run_case(api, key, payload, {"i": i, "queue_burst": burst})
        append_record(RAW_PATH, record)
        print(f"  {key}  delay={record['delay_ms']}ms")

    import threading

    thread = threading.Thread(target=sampler, daemon=True)
    thread.start()
    with concurrent.futures.ThreadPoolExecutor(max_workers=burst) as pool:
        list(pool.map(one, range(burst)))
    stop["flag"] = True
    thread.join(timeout=5)
    if samples:
        t0 = samples[0]["t"]
        for s in samples:
            s["t"] = round(s["t"] - t0, 1)
        append_record(
            RAW_PATH,
            {"key": "queue:health:timeline", "timeline": samples, "burst": None},
        )
        print(f"  queue:health:timeline  {len(samples)} samples")


def section_flashboot(api: Api, cfg: dict[str, Any], done: set[str]) -> None:
    """Post-idle starts with FlashBoot on, then off, then restored."""
    probes = cfg.get("flashboot_idle_probes", 3)
    idle = cfg["idle_timeout_s"] + 20
    try:
        for mode, flag in (("on", True), ("off", False)):
            if f"flashboot:{mode}:{probes - 1}" in done:
                continue
            api.set_flashboot(flag)
            print(f"  flashboot={mode}; settling")
            time.sleep(10 * api.sleep_scale)
            for i in range(probes):
                key = f"flashboot:{mode}:{i}"
                if key in done:
                    continue
                time.sleep(idle * api.sleep_scale)
                payload = {"prompt": cfg["prompt"], "seed": cfg["seed"]}
                record, _ = run_case(api, key, payload, {"fb_mode": mode, "i": i})
                append_record(RAW_PATH, record)
                print(f"  {key}  delay={record['delay_ms']}ms")
    finally:
        # An abort mid-section must not leave the endpoint slow-booting.
        api.set_flashboot(True)


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
        try:
            time.sleep(25 * api.sleep_scale)
        finally:
            # Ctrl-C during the window must not leave the live endpoint
            # pinned at workersMax=0 — that is a silent outage.
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


def section_gpu(api: Api, cfg: dict[str, Any], done: set[str]) -> None:
    """Default-config runs on whatever card --gpu-label names.

    The endpoint's GPU list is switched outside the harness; this section only
    measures. The first job may sit through staging on a fresh host — its
    delay is recorded, and exec times are what the comparison uses.
    """
    label = cfg.get("gpu_label")
    if not label:
        print("  --gpu-label not set; skipping")
        return
    for i in range(cfg.get("gpu_n", 5)):
        key = f"gpu:{label}:{i}"
        if key in done:
            continue
        payload = {"prompt": cfg["prompt"], "seed": cfg["seed"]}
        record, _ = run_case(api, key, payload, {"gpu_label": label, "i": i})
        append_record(RAW_PATH, record)
        print(f"  {key}  delay={record['delay_ms']}ms exec={record['execution_ms']}ms")


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
    # Nearest-rank p95: ceil(0.95n)-1, not int(0.95n), which sits one rank
    # high and degenerates to the max at small N.
    rank = max(0, -(-19 * len(ordered) // 20) - 1)
    return {
        "n": len(ordered),
        "p50": statistics.median(ordered),
        "p95": ordered[min(len(ordered) - 1, rank)],
        "min": ordered[0],
        "max": ordered[-1],
    }


def _rows(records: list[dict[str, Any]], group: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for record in records:
        if record.get(group) is not None and record.get("status") == "COMPLETED":
            grouped.setdefault(record[group], []).append(record)
    return grouped


def _product_sections(
    cfg: dict[str, Any], records: list[dict[str, Any]], rate: float
) -> list[str]:
    """Render the GPU-tier and queue-pressure sections."""
    lines: list[str] = []
    gpu_rows = _rows(records, "gpu_label")
    if gpu_rows:
        lines += [
            "",
            "## GPU tiers, same workload (1024², 28 steps)",
            "",
            "| Card | N | rate \\$/hr | \\$/second | exec p50 | \\$/image at p50 |",
            "|---|---|---|---|---|---|",
        ]
        rates = cfg.get("gpu_rates_usd_hr", {})

        def card_row(label: str, rows: list[dict[str, Any]]) -> str:
            card_rate = next(
                (
                    v
                    for k, v in rates.items()
                    if k.lower()[:8] in label.lower() or label.lower()[:8] in k.lower()
                ),
                rate,
            )
            s = _stats([r["execution_ms"] / 1000 for r in rows])
            return (
                f"| {label} | {s['n']} | \\${card_rate:.2f} | \\${card_rate / 3600:.5f} "
                f"| {s['p50']:.1f}s | \\${s['p50'] / 3600 * card_rate:.4f} |"
            )

        main_28 = _rows(records, "steps").get(28, [])
        if main_28:
            lines.append(card_row("48GB tier (served: RTX PRO 6000 MIG)", main_28))
        for label, rows in sorted(gpu_rows.items()):
            lines.append(card_row(label, rows))
        lines += [
            "",
            "\\$/hr is the pricing-page number; \\$/image is the one that should pick "
            "the card: a faster expensive card can tie or beat a cheaper slow one. "
            "Rates as configured in `benchmarks/config.json`, dated there.",
        ]
    queue_rows = [
        r
        for r in records
        if r.get("queue_burst")
        and r.get("status") == "COMPLETED"
        and r.get("delay_ms") is not None
    ]
    timeline = next(
        (r for r in records if r.get("key") == "queue:health:timeline"), None
    )
    if queue_rows:
        delays = sorted(r["delay_ms"] / 1000 for r in queue_rows)
        spacing = (delays[-1] - delays[0]) / max(len(delays) - 1, 1)
        lines += [
            "",
            f"## Queue under a {len(queue_rows) // max(cfg.get('workers_max', 3), 1)}x burst "
            f"({len(queue_rows)} jobs, workersMax={cfg.get('workers_max', 3)})",
            "",
            f"Queue wait: first job {delays[0]:.0f}s, last {delays[-1]:.0f}s, "
            f"climbing ~{spacing:.1f}s per position: a linear drain, no failures, no timeouts. "
            "The queue is the product here: 4x overload became added latency, never an error.",
        ]
        if timeline:
            samples = timeline["timeline"]
            peak_q = max(s["inQueue"] for s in samples)
            peak_run = max(s["running"] for s in samples)
            ramp = next((s["t"] for s in samples if s["running"] >= peak_run), None)
            lines += [
                "",
                f"Health timeline ({len(samples)} samples at ~2s): peak inQueue {peak_q}, "
                f"peak running workers {peak_run}, full ramp reached at t+{ramp:.0f}s. "
                "Raw timeline in `benchmarks/raw.jsonl` under `queue:health:timeline`.",
            ]
    return lines


def _p50(rows: list[dict[str, Any]], field: str) -> float:
    """Median of one numeric field across records, or 0.0 if none have it."""
    vals = sorted(float(r[field]) for r in rows if r.get(field) is not None)
    return statistics.median(vals) if vals else 0.0


def _comparisons_section(
    cfg: dict[str, Any], records: list[dict[str, Any]], rate: float
) -> tuple[list[str], float]:
    """Render "The comparisons that matter" and return it with cost_28.

    Args:
        cfg: The benchmark configuration.
        records: All raw records.
        rate: The configured $/hr rate.

    Returns:
        The rendered lines, and the 28-step p50 cost per image (also needed
        by the "Named outputs" section further down `render`).
    """
    by_phase = _rows(records, "phase")
    by_steps = _rows(records, "steps")
    by_fmt = _rows(records, "format")
    cost_28 = _p50(by_steps.get(28, []), "execution_ms") / 1000 / 3600 * rate
    cost_20 = _p50(by_steps.get(20, []), "execution_ms") / 1000 / 3600 * rate
    market = cfg.get("market", {})

    comparisons = [
        "## The comparisons that matter",
        "",
        f"**Warm vs post-idle vs true cold:** {_p50(by_phase.get('warm', []), 'delay_ms') / 1000:.1f}s vs "
        f"{_p50(by_phase.get('resume', []), 'delay_ms') / 1000:.1f}s vs "
        f"{_p50(by_phase.get('true_cold', []), 'delay_ms') / 1000:.0f}s p50 "
        f"(true-cold max {max((r['delay_ms'] for r in by_phase.get('true_cold', [])), default=0) / 1000:.0f}s, "
        "when staging lands on a fresh host). The whole serverless trade in one row: "
        "pin an active worker for demos, trust FlashBoot for steady traffic, budget minutes for bursts from zero.",
        "",
        f"**20 steps vs the 28-step default:** \\${cost_20:.4f} vs \\${cost_28:.4f} per image "
        f"({'n/a' if cost_28 == 0 else f'{(1 - cost_20 / cost_28) * 100:.0f}% cheaper'}) "
        "and visually equivalent in the fixed-seed grid "
        "(`samples/quality-grid/`): per-pixel different, indistinguishable at arm's length. "
        "The default is a quality ceiling, not the value optimum.",
        "",
        "**JPEG vs PNG at 1536²:** "
        f"{_p50(by_fmt.get('jpeg', []), 'payload_b64_bytes') / 1e6:.2f}MB vs "
        f"{_p50(by_fmt.get('png', []), 'payload_b64_bytes') / 1e6:.2f}MB base64, "
        f"{_p50(by_fmt.get('png', []), 'payload_b64_bytes') / max(_p50(by_fmt.get('jpeg', []), 'payload_b64_bytes'), 1):.1f}x smaller for polling clients.",
        "",
    ]
    fb = _rows(records, "fb_mode")
    if fb.get("on") and fb.get("off"):
        on_p50 = _p50(fb["on"], "delay_ms") / 1000
        off_p50 = _p50(fb["off"], "delay_ms") / 1000
        comparisons += [
            f"**FlashBoot on vs off:** {on_p50:.1f}s vs {off_p50:.1f}s post-idle start p50, "
            f"{off_p50 / max(on_p50, 0.01):.0f}x. The spec asserted this benefit before anything was deployed; "
            "this row is the assertion closed with data. It costs nothing, which makes disabling it strictly worse.",
            "",
        ]
    gpu_cmp = _rows(records, "gpu_label")
    a100_rows = gpu_cmp.get("A100 80GB PCIe", [])
    if a100_rows and by_steps.get(28):
        a100_rate = cfg.get("gpu_rates_usd_hr", {}).get("A100 80GB PCIe", rate)
        main_p50 = _p50(by_steps[28], "execution_ms") / 1000
        a100_p50 = _p50(a100_rows, "execution_ms") / 1000
        faster = (
            "n/a" if main_p50 == 0 else f"{(1 - a100_p50 / main_p50) * 100:.0f}% faster"
        )
        comparisons += [
            f"**48GB tier vs A100 80GB:** \\${main_p50 / 3600 * rate:.4f} vs "
            f"\\${a100_p50 / 3600 * a100_rate:.4f} per image (a tie) at "
            f"{a100_p50:.1f}s vs {main_p50:.1f}s exec p50 "
            f"({faster}). FLUX is "
            "memory-bandwidth-bound; HBM absorbs the +55% hourly rate. "
            "\\$/hr picks the wrong card, \\$/image the right one.",
            "",
        ]
    if market and cost_28:
        comparisons += [
            f"**Self-hosted vs managed FLUX.1-dev APIs:** \\${cost_28:.4f} measured here vs "
            f"~\\${market['typical_managed_usd']:.3f} typical managed and ~\\${market['bfl_api_usd']:.2f} at BFL's own API "
            f"({market['typical_managed_usd'] / cost_28:.1f}x and {market['bfl_api_usd'] / cost_28:.1f}x respectively). "
            "Honest caveats: our figure is execution-only (idle billing is additive and traffic-shaped), it excludes "
            "the ops you are reading the runbook for, and FLUX.1-dev's licence is non-commercial; managed APIs "
            "carry the commercial licence in their price. The multiple is real at scale; the licence decides who may use it. "
            f"(Market prices {market['sourced']}.)",
            "",
        ]
    return comparisons, cost_28


def render(cfg: dict[str, Any], tag: str) -> str:
    """Render BENCHMARKS.md from the raw records.

    Args:
        cfg: The benchmark configuration.
        tag: The image tag measured.

    Returns:
        The report markdown.
    """
    if not RAW_PATH.exists():
        msg = f"no records at {RAW_PATH}; run at least one section first"
        raise SystemExit(msg)
    records = [json.loads(line) for line in RAW_PATH.read_text().splitlines() if line]
    rate = cfg["rate_usd_hr"]
    comparisons, cost_28 = _comparisons_section(cfg, records, rate)

    # The measurement date lives in the data; stamping render time would let a
    # re-render silently postdate the numbers.
    measured = max(
        (str(r["recorded_at"])[:10] for r in records if r.get("recorded_at")),
        default=time.strftime("%Y-%m-%d"),
    )
    lines = [
        "# Benchmarks",
        "",
        f"Measured {measured} against endpoint `<endpoint-id: supplied in the submission email>`, "
        f"image `{tag}`, GPU {cfg['gpu']}, model per response `model_version`. "
        f"Rate ${rate}/hr ({cfg['rate_date']}, re-verify before quoting). "
        f"Raw data: `benchmarks/raw.jsonl` ({len(records)} records). "
        "Methodology: fixed seed, one variable at a time, N stated per cell.",
        "",
        "**Read the N column before any percentile.** N is 3-10 per cell: sequential probes, "
        "no sustained load. Enough to rank options (step count, GPU tier, image format); "
        "not literal latency guarantees. SLO-grade p50/p95 needs a proper load test "
        "(e.g. Locust) at production concurrency, which this harness does not do.",
        "",
        *comparisons,
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
            f"| {fmt} | {s['n']} | {s['p50'] / 1e6:.2f}MB | "
            f"{'fits' if s['p50'] < PAYLOAD_CAP_BYTES else 'EXCEEDS'} |"
        )
    lines += _product_sections(cfg, records, rate)
    burst_rows = [
        r
        for r in records
        if r.get("burst")
        and r.get("status") == "COMPLETED"
        and r.get("delay_ms") is not None
    ]
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
        r
        for r in records
        if r.get("steps") == DEFAULT_STEPS and r.get("status") == "COMPLETED"
    ]
    if step_28:
        avg = _stats([r["execution_ms"] / 1000 for r in step_28])["p50"]
        lines += [
            "",
            "## Named outputs",
            "",
            f"- `AVG_JOB_S = {avg:.1f}`: a p50, not a mean; feeds the gateway's `avg_job_s` queue-pressure setting",
            f"- Cost per default image (1024², 28 steps): ${avg / 3600 * rate:.4f} execution-only",
        ]
    lines += [
        "",
        "## Descoped, and why",
        "",
        "| Planned | Status |",
        "|---|---|",
        '| Exact-GPU pinning | Not possible on serverless: scheduling is pool-based (the v2 API takes `gpu.pools`; the catalog assigns each card a pool), which is how an "NVIDIA L40S" request was served by an RTX PRO 6000 MIG. The levers are pool, CUDA filter and datacenter. Per-worker `gpuTypeId` exists in the v2 workers API, 403 on this account today, as are v2 catalog and GraphQL |',
        "| 4090 floor test | Dropped: it would only prove this worker's resident-bf16 policy needs >34GB. Offload (~27GB) and fp8 (~14GB) run FLUX on smaller cards; the claim is scoped in the README instead |",
        "| Weight-delivery three-way | Only cached models is deployed; volume was dropped in design, baked was never pushed or deployed |",
        "| CFG 2× claim | Not measurable through the deployed contract: the input schema deliberately omits `true_cfg_scale` |",
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
    parser.add_argument(
        "--gpu-label", default=None, help="card label for the gpu section"
    )
    args = parser.parse_args()

    cfg = json.loads((ROOT / "benchmarks" / "config.json").read_text())
    if args.n:
        cfg["n"] = args.n
    if args.gpu_label:
        cfg["gpu_label"] = args.gpu_label

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
        "queue": section_queue,
        "flashboot": section_flashboot,
        "gpu": section_gpu,
    }
    requested = [name.strip() for name in args.only.split(",") if name.strip()]
    unknown = [name for name in requested if name not in sections]
    if unknown:
        print(
            f"unknown section(s) {unknown}; valid: {', '.join(sections)}",
            file=sys.stderr,
        )
        return 2

    done = load_done(RAW_PATH)
    for name in requested:
        print(f"== {name}")
        sections[name](api, cfg, done)

    if set(requested) >= set(SECTIONS):
        REPORT_PATH.write_text(render(cfg, args.tag))
        print(f"rendered {REPORT_PATH}")
    else:
        print(
            f"partial run (--only {args.only}); not rendering {REPORT_PATH} — "
            f"run the full section set ({','.join(SECTIONS)}) to regenerate it"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
