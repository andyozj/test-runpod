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
scripts/fetch_weights.py   filtered download for the baked image
Dockerfile        one file, both variants; BAKE_WEIGHTS toggles
test_input.json   payload for RunPod's local test harness
```

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

Build and deploy: [`docs/RUNBOOK.md`](../docs/RUNBOOK.md).
