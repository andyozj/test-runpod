# Platform-alignment patch note — for the session editing 00 / 06 / worker code

Source: review of current RunPod docs (get-started, handler-functions, endpoint-configurations,
job-operations, local-testing, model-caching) on 2026-08-05. Delete this file once merged.

Already applied elsewhere — do not redo: 01-worker (`/runsync` demo section, result-retention
windows, GPU-priority + demo-min-worker rows, ~34GB VRAM correction, cached path in Weight path),
07-testing (SDK-harness section, `/runsync` E2E row, sha256 across all variants), 09-benchmarks
(three-way weight delivery, 4090 fails at load, cache-staging row), README (`/runsync` one-liner,
VRAM, gateway status row).

## 00-overview.md

- Weights scope row + "Why two weight-delivery variants": add **cached models** as a third
  benchmarked variant. RunPod's stated recommendation for HF-hosted models: platform pre-stages
  the repo on hosts before worker start, download unbilled, cold start seconds, no datacenter
  pin, no storage cost. Reuses the ~10GB volume image; enabling it is the endpoint's **Model**
  field + HF token (gated repos supported). Caveat: stages the whole repo — duplicate
  `flux1-dev.safetensors` included, ~56GB not ~33GB. One cached model per endpoint.
- "Async as the default": add one line that the raw endpoint's `/runsync` is the demo path
  (detail now lives in 01).
- Corrections list: append — cached models added; endpoint management via REST API; SDK harness
  adopted for Pod smoke tests; `/runsync` adopted for the demo.

## 06-build-deploy.md

- **Endpoint config as code: GraphQL `saveEndpoint` → REST API.** RunPod's REST API is the
  current management surface; GraphQL is legacy-supported. `scripts/apply_endpoint.py` should
  target REST. (08-production-readiness.md line ~27 also says `saveEndpoint`.)
- **Layer splitting for the baked image.** One ~33GB weights layer risks registry per-layer
  caps (verify GHCR's cap before first push; largest community-proven baked image ≈35GB total)
  and serialises pull+decompress on scale-up. Split `fetch_weights.py` into multiple RUN steps
  via `allow_patterns` — per component, per shard for the 23.8GB transformer.
- Endpoint config fields to declare in `deploy/endpoints/*.yaml`: GPU priority list (up to 3
  types), FlashBoot, job TTL (default 24h; expiry mid-run ⇒ job vanishes, `/status` 404),
  execution timeout, active workers (1 during the demo window). Add `cached.yaml` if the third
  benchmark endpoint is kept.
- "Building on a Pod" smoke test: commit `test_input.json`; runbook uses
  `python -m worker.handler` (one job, real GPU, no queue) and `--rp_serve_api` (local FastAPI
  simulator, `POST /runsync`) between "image pushed" and "endpoint created".
- Reconciler/polling note (02 owns it): async results are retained **30 min** after completion,
  sync **1 min** — the reconciler must observe terminal states within that window.

## 06 / runbook — torch wheel is cu130

`uv.lock` resolves torch 2.13.0+cu130 (CUDA 13 wheels, bundled libs — the 12.4 base image is
irrelevant to this). cu13 needs host driver r580+. Either set the endpoint's Allowed CUDA
Versions filter to 13.x, or pin torch to a cu12x wheel via a pytorch index in uv config.
Verify on the Pod before deploying; record the choice in the runbook.

## 06 — cached-models variant needs no code

Pinned revision ⇒ static snapshot path. Endpoint env for the cached variant:
`WEIGHTS_PATH=/runpod-volume/huggingface-cache/hub/models--black-forest-labs--FLUX.1-dev/snapshots/<sha from contracts/model-revision.txt>`
plus Model field = black-forest-labs/FLUX.1-dev + HF token (gated). Optionally set
`HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` on all endpoints — the docs' belt-and-braces
against silent runtime downloads; the worker already uses local_files_only=True.

## 00 or 06 — one line on Runpod Flash, considered and rejected

Flash (docs.runpod.io/flash) is the no-Docker path: `@Endpoint`-decorated local Python, uploaded
as a deployment package capped at 1.5GB. Rejected here because the brief explicitly grades
`handler.py` + a Docker image containing the model — Flash has neither — and its package cap
cannot carry weights anyway (its own docs say use network volumes). The cap does not apply to
docker-registry serverless endpoints. Worth stating so a reviewer sees the road not taken.

## worker code — build blockers: FIXED 2026-08-05 evening, do not redo

All four fixed in the working tree (Dockerfile COPY paths, `fetch_weights.py` upward search +
`--revision` flag, single-interpreter uv install pinned to the lock via `uv export --frozen
--extra gpu`, root `.dockerignore` safe for both root-context builds). `worker/test_input.json`
added and copied into the image. `make check` green (67 tests). Local amd64 volume-variant
build running as verification. Original list kept below for reference only.

## worker code — original blocker list (reference)

1. `worker/Dockerfile:37` — `COPY pyproject.toml uv.lock ./` but build context is repo root
   (`make build-*` uses `-f worker/Dockerfile .`); files are at `worker/`. Build fails at first COPY.
2. `worker/scripts/fetch_weights.py:57` — `parents[2]` resolves to `/` inside the image
   (script lands at `/app/scripts/`); contracts are at `/app/contracts`. Baked build fails after
   the deps layer. Fix: mirror repo layout in the image, or add the `--revision` flag 06 already
   references.
3. `worker/Dockerfile:26-42` — `python3-pip` drags in python3.10; `uv pip install --system` may
   target it while `CMD python` is 3.11. Use `uv pip install --python /usr/bin/python3.11`,
   drop `python3-pip`. Also: uv.lock is copied but unused — deps install from `>=` ranges;
   `uv export --extra gpu --frozen` for a pinned install.
4. No `.dockerignore` — context hauls both `.venv`s and `.git`.

Cheap verification of 1/3/4 without credits:
`docker buildx build --platform linux/amd64 --build-arg BAKE_WEIGHTS=false -f worker/Dockerfile .`
then `docker run --rm <img> python -c "import torch, diffusers, worker.handler"`.
