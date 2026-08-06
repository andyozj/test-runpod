# Runbook

Operational procedures. Written to be followed under time pressure by someone who did not write them.

## Deploy record

Update on every deploy. **A rollback that begins with "work out which tag was good" is not a rollback.**

| Date | Endpoint | Tag | Previous (rollback target) | Notes |
|---|---|---|---|---|
| 2026-08-06 | `flux-worker-cached` (`7jrg4nu4b47fsv`) | `0.1.0-44c9643-slim` | — (first deploy) | Image digest `bfc09415c350`. Cached model + HF token set in console post-create |
| 2026-08-06 | `flux-worker-cached` (`7jrg4nu4b47fsv`) | `0.1.0-72e537d-slim` | `0.1.0-44c9643-slim` | Error envelopes JSON-encoded; GPU list narrowed to L40S only. 7/7 e2e green post-roll |

## Prerequisites

| Requirement | Verify with |
|---|---|
| FLUX.1-dev licence accepted, `HF_TOKEN` set | `make weights-check` — lists files rather than 403 |
| RunPod credits | Console billing page |
| GHCR write access | `echo $GHCR_TOKEN \| docker login ghcr.io -u USERNAME --password-stdin` |
| Docker with buildx | `docker buildx version` |

The GHCR token must be a **classic** PAT with `write:packages`. Fine-grained tokens have no packages scope and fail login — the permissions dropdown simply lacks the entry, which reads as "you're looking in the wrong place" rather than "wrong token type" (2026-08-06, an hour).

`make weights-check` proves both build-blockers at once — the gated repo is reachable, and the ~24GB of duplicate single-file weights are excluded. Downloads nothing.

## Build and push

**Build locally.** The image is ~2.9GB: no weights, and no CUDA base image, because the torch wheel carries its own libraries. There is no Pod in this procedure — an earlier revision baked a 45GB image and needed one; that stopped being true.

```bash
export IMAGE=ghcr.io/OWNER/flux-worker
export TAG=0.1.0-$(git rev-parse --short HEAD)

make build-slim IMAGE=$IMAGE TAG=$TAG      # ~2.9GB, no weights
docker push $IMAGE:$TAG-slim
```

`--platform linux/amd64` is set in the Makefile. Building on an arm64 Mac without it produces an image RunPod cannot run, and the failure presents as a worker that starts and immediately dies.

### Smoke test before pushing

```bash
docker run --rm --platform linux/amd64 $IMAGE:$TAG-slim \
  python -c "import torch, diffusers, worker.handler; print('imports ok')"
```

Without a GPU that is as far as it goes locally, and it is worth doing: it catches a broken install, the wrong interpreter, and an architecture mismatch — three failures that would otherwise appear as a dead worker on a paid endpoint.

The image also ships `test_input.json`, so on a GPU host `docker run --rm --gpus all $IMAGE:$TAG-slim` runs one real job end to end through the same `handler` the endpoint calls. First thing to try if the endpoint misbehaves.

## Deploy

Configuration lives in `deploy/endpoints/*.yaml` and is applied through RunPod's REST API. The script upserts a **template** (image, environment) and then an **endpoint** (GPUs, scaling, timeouts, filters) — RunPod models those separately, so a deploy is two idempotent calls, each looked up by name.

```bash
export RUNPOD_API_KEY=...

python scripts/apply_endpoint.py --config deploy/endpoints/cached.yaml \
    --tag $TAG-slim --dry-run      # prints both payloads, calls nothing
python scripts/apply_endpoint.py --config deploy/endpoints/cached.yaml \
    --tag $TAG-slim
```

On updates the script bounces `workersMax` 0 → configured automatically, because FlashBoot workers do not re-pull on release (diagnosis table below). Expect ~30s of `409` on submissions during the bounce; `--no-bounce` skips it when stale workers are acceptable.

Then, on that endpoint in the console:

1. **Model** = `black-forest-labs/FLUX.1-dev`
2. **HuggingFace token** — the repo is gated; staging fails without one
3. Leave `WEIGHTS_PATH` **unset**. The worker falls through to `MODEL_CACHE_ROOT` only when it is absent, so setting it would win and the cache would go unused

The Model field is **console-only** — verified 2026-08-06 by searching the REST OpenAPI (23 paths, zero model fields). Until the API grows it, this is the one setting config-as-code cannot carry, and skipping it produces the exact all-workers-unhealthy signature below.

**The image must be pullable before workers can boot.** A GHCR package is private by default even in a public account; the worker's system log shows `error pulling image: ... unauthorized`. Make the package public (github.com/users/OWNER/packages/container/flux-worker/settings) or attach a registry credential. Prove it the way RunPod's daemon sees it — repo visibility is a different setting and proves nothing:

```bash
TOKEN=$(curl -s 'https://ghcr.io/token?scope=repository:OWNER/flux-worker:pull' | jq -r .token)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  -H 'Accept: application/vnd.oci.image.index.v1+json' \
  https://ghcr.io/v2/OWNER/flux-worker/manifests/TAG        # 200 = pullable
```

Record the tag and the previous tag in the table above.

### Warm it before demonstrating

```bash
curl -s -X POST "https://api.runpod.ai/v2/$ENDPOINT_ID/runsync" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" -H "Content-Type: application/json" \
  -d '{"input": {"prompt": "warmup", "num_inference_steps": 4}}' > /dev/null
```

A cold worker spends 30-60s loading before it generates anything. `/runsync` holds the connection through that and may time out — which would make the demo look broken on the first call. Setting **active workers to 1** for the demo window removes cold starts entirely and bills continuously; set it back to 0 afterwards.

## Verify

```bash
export RUNPOD_ENDPOINT_ID=<from the apply output>
python client/generate.py "a red fox in falling snow" --out fox.png
```

Expect a job id, progress ticking to 100%, then a PNG with its seed and timings.

Commit the image with its prompt and seed. A result nobody can reproduce is a screenshot, not evidence.

## Rollback

```bash
python scripts/apply_endpoint.py --config deploy/endpoints/cached.yaml --tag <previous-tag>
```

Works only because tags are immutable. With `latest` the previous image no longer exists to roll back to, which is why the script refuses to deploy it.

## Diagnosis

Ordered by how often each is actually the cause.

| Symptom | Likely cause | Check |
|---|---|---|
| All workers `unhealthy`, system log ends at `worker is ready`, container log empty | Model field not set — the container crashed at warm-up before the SDK attached, and cold-start output is not always surfaced | Reproduce locally: `docker run --rm --platform linux/amd64 $IMAGE:$TAG-slim python -m worker.handler` prints exactly what the worker would have — the resolver traceback names the missing mechanism (2026-08-06: this was the cause) |
| System log: `error pulling image ... unauthorized` | GHCR package still private — the default, even in a public account | The anonymous-pull check above; package settings, not repo settings |
| New release deployed, behaviour unchanged | **Documented**: releases roll gradually and old versions serve "until they are replaced", with no time bound — API and console updates alike. The narrow gap: a **FlashBoot-retained** worker is neither "idle" (terminated immediately) nor "processing", and one resumed old code on the next job (2026-08-06) | The bounce — the documented remedy for strict consistency. `apply_endpoint.py` does it automatically. The Workers tab marks old-version workers "terminating" during a rollout |
| Worker starts and dies immediately | Image built for arm64 | `docker inspect $IMAGE:$TAG-slim \| grep Architecture` — must be `amd64` |
| Every job fails at startup | Weights not found, or cache holds a different revision | Worker logs — `weights_resolved` on success; otherwise a message naming all three mechanisms |
| Refuses to start, revision mismatch | Cache staged a snapshot other than the pinned one | Reconcile `contracts/model-revision.txt` with what the message reports |
| Jobs stuck `IN_QUEUE`, no workers | No GPU matching `gpuTypeIds` **and** `allowedCudaVersions` | `GET /v2/{id}/health` → `workers.running` stays 0. Widen the GPU list, or the CUDA filter |
| Torch fails to initialise CUDA | cu130 wheel on an older host driver | Narrow `allowed_cuda_versions` to 13.x, or repin torch to cu12x and rebuild |
| First job slow, later fine | Normal cold start | `pipeline_loaded` duration in worker stdout |
| `OOM` | Resolution too high for the GPU | Response carries `refresh_worker`; retry lower |
| Gateway 503 | Circuit breaker open | `GET /health/detailed` |
| Gateway 429 | Queue at the `max_workers` ceiling | `Retry-After` header; `/health/detailed` shows depth |
| Job never resolves | Reconciler died | `/health/detailed` → `reconciler.last_run_s_ago` climbing |
| `/status` 404 for a known job | Result retention expired — 30 min async, 1 min sync | Nothing to recover; resubmit |

### What is visible from where

RunPod serverless supports no sidecars, so this split is fixed:

| Signal | Source |
|---|---|
| Generation latency, seed, OOM, guardrail blocks | Worker stdout, via RunPod logs |
| Pipeline load duration | Worker stdout |
| Queue depth, worker counts | RunPod API only — the container cannot see them |
| **Cold-start failures** | **Neither.** The container never reaches the handler, so nothing is emitted. Detectable only by a synthetic probe that never completes |

That last row is the one that wastes time if you do not know it in advance.

## Teardown

1. Delete the endpoints and their templates.
3. Revoke the `HF_TOKEN` if it was created for this exercise.

Cached models need no teardown: nothing was provisioned and nothing bills for storage.
