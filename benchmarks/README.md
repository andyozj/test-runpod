# Benchmarks (spike)

Measurement harness for the deployed endpoint: steps, resolution, payload, concurrency, cold starts, queueing, FlashBoot. Beyond the brief (the endpoint is callable without it), but the numbers quoted in the root README come from its output. Methodology (fixed seed, one variable at a time, N stated per cell) and the N=3-10 caveat (ranks options, not latency guarantees) are in [`BENCHMARKS.md`](../BENCHMARKS.md)'s header.

Intent: curiosity. The endpoint was live and the obvious questions had no measured answers: what a step costs, when cold start hurts, whether the 48GB tier beats an A100 on $/image. It characterizes the endpoint; it does not load-test it.

| File | What |
|---|---|
| `harness.py` | Runner and renderer, stdlib only |
| `config.json` | Section parameters and sample counts |
| `raw.jsonl` | Every record taken (156 committed), the evidence behind the report |

## Run

```bash
export RUNPOD_API_KEY=... RUNPOD_ENDPOINT_ID=...   # --fake needs neither
python benchmarks/harness.py --tag <image-tag> --fake              # dry run, no GPU
python benchmarks/harness.py --tag <image-tag>                     # full sweep, ~1h GPU
python benchmarks/harness.py --tag <image-tag> --only steps,cold --n 2
```

| Flag | Effect |
|---|---|
| `--tag` | Required. The image tag being measured; recorded on every row and printed in the report |
| `--fake` | Substitutes a fake API. No credentials, no GPU, no spend — the way to check a change to the harness |
| `--only` | Comma-separated subset. Default and render set: `warmup,steps,resolution,payload,concurrency,cold,queue,flashboot`. `gpu` is a ninth, runnable only by naming it — it measures a different card, so it is not part of a default sweep. An unknown name exits 2 and lists the valid ones |
| `--n` | Override the per-config sample count from `config.json` |
| `--gpu-label` | Card label for the `gpu` cross-tier section |

Records append to `raw.jsonl` keyed by (section, config, index), so a run that dies partway resumes without repeating completed work.

Outputs written outside this directory:

- [`BENCHMARKS.md`](../BENCHMARKS.md): rendered from `raw.jsonl`, never hand-written — an edit there is overwritten by the next full run. Only a full-section run renders it; an `--only` subset prints the section list it would need instead, because rendering reads all of `raw.jsonl` and a partial run would publish sections measured against different images
- `samples/quality-grid/`: the step-sweep images, one per entry in `steps_sweep`

## `config.json`

One file, no CLI equivalents. What each knob moves:

| Key | What it sets |
|---|---|
| `prompt`, `seed` | Fixed across every section, so runs are comparable |
| `n` | Default samples per config; `--n` overrides |
| `steps_sweep`, `resolution_sweep` | The x-axis of the steps and resolution sections |
| `payload_formats`, `payload_resolution`, `payload_n` | Response-size comparison: PNG vs JPEG at one resolution |
| `concurrency_burst`, `queue_burst` | Simultaneous submissions for the concurrency and queue sections |
| `cold_cycles`, `idle_timeout_s`, `workers_max` | Cold-start section: how many scale-to-zero cycles, how long past the endpoint's idle timeout to wait (`+20s`), and the ceiling `workersMax` is restored to after being driven to 0 — it must match the endpoint's real configuration |
| `flashboot_idle_probes` | Post-idle resume probes |
| `gpu`, `gpu_rates_usd_hr`, `rate_usd_hr`, `rate_date` | Card labels and \$/hr for the cost columns. Rates are hand-entered and dated; stale rates make the \$/image figures wrong |
