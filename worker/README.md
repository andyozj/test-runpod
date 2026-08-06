# Worker

The graded deliverable: a RunPod serverless handler running FLUX.1-dev. The repo-level [README](../README.md) documents the API, errors, and weight delivery; this file covers the package.

## Layout

```
src/worker/
  handler.py      RunPod entrypoint: validate → guardrails → infer → encode → progress
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
