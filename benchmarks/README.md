# Benchmarks (spike)

Measurement harness for the deployed endpoint, one variable at a time: steps, resolution, payload, concurrency, cold starts, queueing, FlashBoot. Beyond the brief (the endpoint is callable without it), but the numbers quoted in the root README come from its output. Methodology: [`docs/specs/09-benchmarks.md`](../docs/specs/09-benchmarks.md).

Intent: curiosity. The endpoint was live and the obvious questions had no measured answers: what a step costs, when cold start hurts, whether the 48GB tier beats an A100 on $/image. The harness answers them with small fixed-seed samples. It characterizes the endpoint; it does not load-test it.

Every table states N per row (3-10 per cell, sequential probes). Percentiles at that N rank options; they are not latency guarantees. SLO-grade p50/p95 needs a proper load test (e.g. Locust) at production concurrency, which has not been run.

| File | What |
|---|---|
| `harness.py` | Runner and renderer, stdlib only |
| `config.json` | Section parameters and sample counts |
| `raw.jsonl` | Every record taken (156 committed), the evidence behind the report |

## Run

```bash
export RUNPOD_API_KEY=... RUNPOD_ENDPOINT_ID=...
python benchmarks/harness.py --tag <image-tag> --fake              # dry run, no GPU
python benchmarks/harness.py --tag <image-tag>                     # full sweep, ~1h GPU
python benchmarks/harness.py --tag <image-tag> --only steps,cold --n 2
```

Records append to `raw.jsonl` keyed by (section, config, index), so a run that dies partway resumes without repeating completed work.

Outputs written outside this directory:

- [`BENCHMARKS.md`](../BENCHMARKS.md): rendered from `raw.jsonl`, never hand-written. Only a full-section run renders it; an `--only` subset prints the section list it would need instead, because rendering reads all of `raw.jsonl` and a partial run would publish sections measured against different images
- `samples/quality-grid/`: the step-sweep images
