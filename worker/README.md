# Worker

The graded deliverable: a RunPod serverless handler running FLUX.1-dev. The repo-level [README](../README.md) documents the API, errors, and weight delivery; this file covers the package.

## What it does

One job in, one image out. The contract, ahead of the mechanism:

- **Generate.** Accept one validated generation request, return one image (PNG or JPEG, base64) — one job at a time per worker, `concurrency_modifier: 1`.
- **Enforce content policy, fail closed, at both stages.** A prompt check before any GPU time and an image check on the decoded bytes before the image is returned. A guardrail that raises still stops the job; it reports `INFERENCE_FAILED`, not a block, because a classifier crash is not a verdict.
- **Make every image reproducible.** The seed is echoed whether the caller supplied it or the worker drew it, alongside the effective dimensions, steps, guidance, and `model_version` (`{model_id}@{revision}`) — the revision *discovered* from the weights actually loaded, not a pinned constant.
- **Report failure and progress structurally.** Every caller-visible failure is a coded error envelope from `contracts/error-codes.json`; progress is coarse by design, ~10 `progress_update` calls per job at 10-point strides.
- **Resolve its own weights at startup.** `weights.py` tries `WEIGHTS_PATH`, then the model cache, and refuses rather than guess between ambiguous snapshots — so a misconfigured mount fails fast instead of silently downloading ~33GB per cold start.

**Not its job.** Queuing, autoscaling 0→3, retries, caller authentication, and billing. The platform owns those, configured in [`deploy/endpoints/`](../deploy/endpoints/) — not in this code. Per-job mechanics: [§Request lifecycle](#request-lifecycle).

## Layout

```
src/worker/
  handler.py      RunPod entrypoint; holds no inference logic
  schemas.py      input model: validation, dimension snapping
  guardrails.py   prompt/image checks; terms and normalisation tables from
                  contracts/blocklist.json and contracts/normalisation.json
  pipeline.py     injectable FluxPipeline accessor; tests never import torch
  inference.py    the generation call, timings, OOM handling
  weights.py      resolve staged weights, discover the loaded revision
  errors.py       structured error envelope, codes from contracts/error-codes.json
  contracts.py    locate the repo-root contracts/ directory
  settings.py     environment configuration
tests/unit/       97 tests, no GPU
tests/e2e/        7 tests against a live endpoint, marked gpu
scripts/fetch_weights.py   filtered download for the baked image
Dockerfile        one file, both variants; BAKE_WEIGHTS toggles
test_input.json   payload for RunPod's local test harness
```

## Request lifecycle

**Execution model.** `CMD` is `python -m worker.handler`. `main()` warms the pipeline, then `runpod.serverless.start` polls for jobs: one at a time per worker (`concurrency_modifier: 1`, [`deploy/endpoints/`](../deploy/endpoints/)), workers scale 0→3. All GPU setup (weights resolution, FLUX onto CUDA) happens inside that warm-up at container start, memoised in a module global: import loads nothing, and no billed job pays for it.

Per job:

1. **Validate** (`schemas.py`). `job["input"]` → `GenerationRequest`, bounds per the [input table](../README.md#input), `extra="forbid"`. Width and height snap **down** to ×16; the snapped values run and are reported. Failure returns `INVALID_*` before any other work.
2. **Prompt guardrail**, before any GPU time. Match → `PROMPT_BLOCKED`. A guardrail that *raises* → `INFERENCE_FAILED`: still fail-closed, but retryable, since a classifier crash is not a verdict on the prompt.
3. **Seed.** The request's, else `secrets.randbelow(2**31 - 1)`; seeds a CUDA `torch.Generator`.
4. **Infer** (`inference.py`). One pipeline call, `max_sequence_length=512`. Progress goes to `runpod.serverless.progress_update` at 10-point strides plus the final step, ~10 calls per job: each SDK call is a thread, an event loop and a TLS session. OOM → `OOM`; any other exception → `INFERENCE_FAILED`, detail logged, not returned.
5. **Image guardrail** on the decoded bytes. Match → `IMAGE_BLOCKED`, crash → `INFERENCE_FAILED`. Default binding is `NoopImageGuardrail`.
6. **Encode.** PNG, or JPEG at quality 95 after `convert("RGB")`, then base64.
7. **Respond.** `image_base64`, `format`, echoed `seed`, effective dimensions, steps, guidance, `model_version` (`{model_id}@{revision}`, discovered at startup), `timings` (`inference_s`, `encode_s`).

**Errors.** Every caller-visible failure is a `WorkerError` with code, message, suggestion, JSON-encoded into RunPod's `error` string: the platform drops a dict there. Decode documented in [README §Errors](../README.md#errors). OOM also flushes the CUDA cache and sets `refresh_worker: true`; fragmentation outlives the flush, so the worker retires.

**Cold start** is weights-dominated: image pull or cache staging, then `FluxPipeline.from_pretrained` + `.to("cuda")`. The per-job flow contributes nothing. Splits: [BENCHMARKS.md](../BENCHMARKS.md#cold-start-decomposed).

## Contracts

Contracts loaded at runtime from the repo-root [`contracts/`](../contracts/)
directory, resolved by walking up from the module (so the same code works at
`worker/src/worker/` and at `/app/src/worker/` in the image):

| File | Loaded by |
|---|---|
| [`blocklist.json`](../contracts/blocklist.json) | `guardrails.py`, and `tests/e2e/` to pick a term the endpoint must reject |
| [`normalisation.json`](../contracts/normalisation.json) | `guardrails.py` |
| [`error-codes.json`](../contracts/error-codes.json) | `errors.py` |
| [`guardrail-corpus.json`](../contracts/guardrail-corpus.json) | `tests/unit/test_guardrail_corpus.py` |
| [`generation-request.schema.json`](../contracts/generation-request.schema.json) | `tests/unit/test_contracts.py`, asserting `schemas.py` accepts every field with matching types and bounds |

## Develop

From the repo root:

```bash
make install    # uv sync, both packages
make check      # format, lint, types, imports, tests + coverage gate
```

Or here, `uv run pytest`: 97 unit tests, no GPU, no network, no weights (a design constraint; STANDARDS.md §9). GPU dependencies (`torch`, `diffusers`, …) are an optional extra installed only in the image, so the dev environment stays importable without them.

The e2e suite (`tests/e2e/`, 7 cases) runs against the live endpoint and is deselected by default:

```bash
RUNPOD_API_KEY=... RUNPOD_ENDPOINT_ID=... uv run pytest -m gpu tests/e2e
```

The `gpu` marker is what separates the two. `addopts = -m 'not gpu'` deselects
the e2e suite from every default run, so `pytest` locally and `make check` in
CI never reach the endpoint. With `-m gpu` and the credentials missing,
behaviour splits on `CI`: locally the suite skips, in CI it is a hard
`pytest.fail` — as the post-deploy gate in `deploy.yml`, a missing secret must
fail the workflow rather than pass by testing nothing. Markers are `--strict-markers`,
so a typo'd marker is an error, not a silent deselect.

## Run one real job

`test_input.json` sits at the package root, and the Dockerfile copies it into `/app`: both are the working directory the RunPod SDK reads it from. Given a GPU, the handler runs that payload once and exits, through the same code path the endpoint calls:

```bash
uv run python -m worker.handler                  # from worker/, with the gpu extra
docker run --rm --gpus all $IMAGE:$TAG-slim      # or from the built image
```

The payload is a 4-step 1024×1024 JPEG at seed 42. No GPU means no run: the handler still starts, resolves weights, and the resolver traceback names whichever mechanism is missing — which is the fastest reproduction of a worker that dies at warm-up.

Build and deploy: [`docs/RUNBOOK.md`](../docs/RUNBOOK.md).
