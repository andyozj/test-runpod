# Worker

The graded deliverable: a RunPod serverless handler running FLUX.1-dev. The repo-level [README](../README.md) documents the API, errors, and weight delivery; this file covers the package.

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

**Execution model.** The platform starts the container, whose `CMD` is `python -m worker.handler`: `main()` calls `get_pipeline()` and then `runpod.serverless.start({"handler": handler})`, which polls RunPod for jobs. `concurrency_modifier: 1` ([`deploy/endpoints/`](../deploy/endpoints/)) gives one job at a time per worker; workers scale 0→3. Importing the module loads nothing and touches no GPU: weights resolution (`weights.py`) and the FLUX load onto CUDA both sit inside `get_pipeline()`, called from `main()`, so a worker pays them during container start rather than during the first billed job. `pipeline.py` memoises the result in a module global, so no job reloads it.

Then, per job:

1. **Validate.** `handler()` takes `job["input"]` (absent or null becomes `{}`) and validates it into a `GenerationRequest` (`schemas.py`): non-blank prompt ≤2000 chars, width and height 256-1536, steps 1-50, guidance 0-20, seed 0-2³¹-1, `extra="forbid"`. Width and height snap **down** to a multiple of 16, silently, and the snapped values are what inference runs and what the output reports. A validation failure returns the error envelope before any other work, coded `INVALID_DIMENSIONS`, `INVALID_STEPS` or `INVALID_PROMPT` from the offending field, with the first pydantic message as the text.
2. **Prompt guardrail**, before any GPU time. Blocked returns `PROMPT_BLOCKED`. A guardrail that *raises* returns `INFERENCE_FAILED`: fail-closed, the prompt still never reaches the GPU, but a classifier crash is an infra fault and retryable as-is, whereas `*_BLOCKED` is terminal until the prompt changes.
3. **Seed.** `request.seed` when supplied, else `secrets.randbelow(2**31 - 1)`, then `torch.Generator("cuda").manual_seed(seed)`.
4. **Inference** (`inference.py`): one pipeline call at `max_sequence_length=512`. `callback_on_step_end` fires after every denoising step; the handler's reporter forwards to `runpod.serverless.progress_update` only on the final step or once the percentage has advanced a full 10 points, ~10 calls per job instead of 28, because each SDK call spawns a thread, an event loop and a TLS session. A job with no `id`, or a missing `runpod` import, reports nothing. A pipeline exception matching `is_out_of_memory` (class name or "CUDA out of memory") becomes `OOM`; every other exception becomes `INFERENCE_FAILED`, detail logged and not returned.
5. **Image guardrail** on the decoded bytes, before the image leaves the worker. Blocked returns `IMAGE_BLOCKED`, a crash `INFERENCE_FAILED`, same fail-closed split. The default binding is `NoopImageGuardrail`.
6. **Encode**: PNG, or JPEG at quality 95 after `convert("RGB")`, then base64.
7. **Output envelope**: `image_base64`, `format`, `seed` (always echoed, including when randomly chosen), `width`, `height`, `num_inference_steps`, `guidance_scale`, `model_version` (`{model_id}@{revision}`, the revision discovered from the staged snapshot or baked manifest), and `timings` carrying `inference_s` and `encode_s` to 3 decimals.

**Errors.** Every caller-visible failure is a `WorkerError` (`GuardrailBlockedError`, `InferenceError`) holding code, message and optional suggestion. `job_output()` JSON-*encodes* that envelope into RunPod's `error` field because the platform silently drops a dict there; the repo README's [§Errors](../README.md#errors) documents the decode for callers. `OOM` additionally calls `torch.cuda.empty_cache()` and sets `refresh_worker: true`, retiring the worker because fragmentation outlives the cache flush.

**Cold start.** Dominated by getting ~34GB of weights onto the GPU: image pull or cache staging, then `FluxPipeline.from_pretrained` plus `.to("cuda")`, which log their own `pipeline_loaded` duration. Nothing in the per-job flow above contributes. Measured splits in [BENCHMARKS.md](../BENCHMARKS.md#cold-start-decomposed).

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
