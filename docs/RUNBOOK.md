# Runbook

Operational procedures. Every step here is meant to be followed under time pressure by someone who did not write it.

## Deploy record

Update this table on every deploy. **A rollback procedure that begins with "work out which tag was good" is not a rollback procedure.**

| Date | Endpoint | Tag | Previous (rollback target) | Notes |
|---|---|---|---|---|
| — | — | — | — | Not yet deployed |

## Prerequisites

| Requirement | How to verify |
|---|---|
| FLUX.1-dev licence accepted | `make weights-check` returns the file list rather than a 403 |
| `HF_TOKEN` with read scope | Same command |
| RunPod credits | Console billing page |
| GHCR write access | `echo $GHCR_TOKEN \| docker login ghcr.io -u USERNAME --password-stdin` |

`make weights-check` proves the two build-blockers at once — the gated repo is reachable, and the ~24GB of duplicate single-file weights are excluded. It downloads nothing.

## Build and push the image

**Do not build locally.** A ~45GB `linux/amd64` build under emulation on an arm64 Mac, followed by a 45GB push over home broadband, is hours of wall-clock and a fragile upload. A RunPod GPU Pod has datacenter bandwidth to both HuggingFace and GHCR.

1. **Provision a Pod.** Any GPU; the build is not GPU-bound, but a GPU pod lets you smoke-test on real hardware before the endpoint exists — which separates "the image is broken" from "the endpoint is misconfigured" while they are still cheap to tell apart.
   - Container disk: **150GB minimum.** The build needs room for the layer cache plus the final image; 100GB will fail partway through the weights layer.

2. **Clone and set secrets.**
   ```bash
   git clone <repo> && cd runpod-flux
   export HF_TOKEN=hf_...
   export TAG=0.1.0-$(git rev-parse --short HEAD)
   export IMAGE=ghcr.io/OWNER/flux-worker
   ```

3. **Build both variants.** The volume image is small and fast; build it first so a deploy is unblocked early.
   ```bash
   make build-volume IMAGE=$IMAGE TAG=$TAG    # ~10GB, minutes
   make build-baked  IMAGE=$IMAGE TAG=$TAG    # ~45GB, 30-60 min, mostly the download
   ```

4. **Smoke test on the Pod before pushing.** Catching a broken image here costs minutes; catching it after a push costs an hour.

   The image ships `test_input.json`, so running the container with no
   arguments executes one real job through the same `handler` the endpoint
   will call — validation, guardrail, generation, encoding. A far stronger
   signal than merely loading the pipeline.

   ```bash
   # volume variant, against the populated volume
   docker run --rm --gpus all -v /runpod-volume:/runpod-volume \
     -e WEIGHTS_PATH=/runpod-volume/flux $IMAGE:$TAG-volume

   # baked variant
   docker run --rm --gpus all $IMAGE:$TAG-baked
   ```

   Expect a `pipeline_loaded` line, then a result carrying `image_base64`,
   `seed: 42` and timings. The fixture uses 4 steps and JPEG so it finishes in
   seconds — it proves the path works, not that the output is beautiful.

   A failure here is almost always one of: weights absent from `WEIGHTS_PATH`,
   the volume not mounted, or a torch/CUDA mismatch. All three are cheap to fix
   on the Pod and expensive to discover on a live endpoint.

5. **Push.** Volume first — it unblocks the deploy.
   ```bash
   docker push $IMAGE:$TAG-volume
   docker push $IMAGE:$TAG-baked
   ```

6. **Terminate the Pod.** It bills by the hour while running.

## Enable cached models (the deployed path)

No population step, no volume, no Pod. RunPod stages the repository on host machines itself.

1. On the endpoint, set **Model** to `black-forest-labs/FLUX.1-dev`.
2. Supply a **HuggingFace token** — the repo is gated, so staging fails without one. The same token used for the build is fine.
3. Leave `WEIGHTS_PATH` **unset** in the endpoint environment. The worker falls through to `MODEL_CACHE_ROOT` only when it is absent; setting it would silently win and the cache would go unused.
4. Deploy with the **volume image tag** — cached and volume share one image.

First worker start pulls a ~10GB image and finds the model already on the host. Staging pulls the whole repository, so the ~24GB duplicate single-file weights come along; that is unbilled and not our disk, but it is why scale-up is measured rather than assumed.

If the worker refuses to start with a revision mismatch, the cache holds a different snapshot than `contracts/model-revision.txt`. That is the resolver working: it will not run a model the response would then misreport.

**Beta.** If it misbehaves, deploy `volume.yaml` with the same tag — no rebuild.

## Populate the network volume (fallback and benchmark only)

Only needed for the volume endpoint. Skip if cached models are working.

1. Create a network volume, ~50GB, in a datacenter that also has L40S capacity. It must be one of the five supporting the S3 API if image storage is ever enabled: `EUR-IS-1`, `EU-RO-1`, `EU-CZ-1`, `US-KS-2`, `US-CA-2`.
2. Attach it to a Pod at `/runpod-volume`.
3. Populate and write the manifest:
   ```bash
   HF_TOKEN=hf_... python worker/scripts/fetch_weights.py --dest /runpod-volume/flux
   cat /runpod-volume/flux/MANIFEST.json     # confirm revision matches contracts/model-revision.txt
   ```
4. Build and push the volume variant: `make build-volume IMAGE=$IMAGE TAG=$TAG`
5. Record the volume id and its datacenter in `deploy/endpoints/volume.yaml`.

**The manifest check is not optional.** A volume populated from a different revision makes the benchmark compare two models rather than two delivery mechanisms, and the result would be meaningless without looking wrong.

## Deploy

```bash
export RUNPOD_API_KEY=...

# The deployed endpoint. Uses the volume image; weights come from the cache.
python scripts/apply_endpoint.py --config deploy/endpoints/cached.yaml --tag $TAG-volume --dry-run
python scripts/apply_endpoint.py --config deploy/endpoints/cached.yaml --tag $TAG-volume

# Then set Model + HF token on the endpoint (console). See the section above.

# Fallback and benchmark endpoints. Optional.
python scripts/apply_endpoint.py --config deploy/endpoints/volume.yaml --tag $TAG-volume
python scripts/apply_endpoint.py --config deploy/endpoints/baked.yaml  --tag $TAG-baked
```

If every job fails instantly, read the startup error — it names all three mechanisms and which one it looked for. The worker fails fast rather than silently re-downloading ~33GB per cold start, which would present as "slow" rather than "broken".

Then **record the tag and the previous tag** in the deploy table above.

Workers pick up the new image on their next cold start; FlashBoot workers drain naturally.

## Verify

```bash
export RUNPOD_ENDPOINT_ID=<from the apply output>
python client/generate.py "a red fox in falling snow" --out fox.png
```

Expect: a job id, progress ticking to 100%, then a saved PNG with its seed and timings. First call after an idle period includes 30-60s of pipeline load.

Commit the image with its prompt and seed — a result nobody can reproduce is a screenshot, not evidence.

## Rollback

```bash
python scripts/apply_endpoint.py --config deploy/endpoints/volume.yaml --tag <previous-tag>-volume
```

Works only because tags are immutable. With `latest`, the previous image no longer exists to roll back to.

## Diagnosis

Ordered by how often each is actually the cause.

| Symptom | Likely cause | Check |
|---|---|---|
| Every job fails immediately | Weights not found, or the cache holds a different revision | Worker logs — `weights_resolved` line on success; a fail-fast message naming all three mechanisms otherwise |
| Worker refuses to start, revision mismatch | Cache staged a snapshot other than the pinned one | Reconcile `contracts/model-revision.txt` with what the message reports as present |
| Jobs stuck `IN_QUEUE`, no workers | No GPU capacity in the pinned datacenter | `GET /v2/{id}/health` → `workers.running` stays 0 |
| First job very slow, later ones fine | Normal cold start | `pipeline_loaded` duration in worker stdout |
| **Every** job slow, no `pipeline_loaded` line | Volume misconfigured; the worker is re-downloading 33GB each start | Worker logs — should have failed fast; if it did not, the startup check is broken |
| `OOM` | Resolution too high for the GPU | Response carries `refresh_worker`; retry lower |
| Gateway returns 503 | Circuit breaker open | `GET /health/detailed` |
| Gateway returns 429 | Queue saturated | `Retry-After` header; `/health/detailed` shows depth |
| Job never resolves | Reconciler died | `/health/detailed` → `reconciler.last_run_s_ago` climbing |

### What is visible from where

RunPod serverless supports no sidecars, so this split is fixed and worth internalising:

| Signal | Source |
|---|---|
| Generation latency, seed, OOM, guardrail blocks | Worker stdout, via RunPod logs |
| Pipeline load duration | Worker stdout |
| Queue depth, worker counts | RunPod API only — the container cannot see them |
| **Cold-start failures** | **Neither.** The container never reaches the handler, so nothing is emitted. Only detectable by a synthetic probe that never completes |

That last row is the one that will waste your time if you do not know it in advance.

## Teardown

1. Delete every endpoint (RunPod console or the API).
2. Delete the network volume — it bills per GB per month for as long as it exists.
3. Terminate any Pods.
4. Revoke the `HF_TOKEN` if it was created for this exercise.

Generated images accumulate with no retention policy ([08](specs/08-production-readiness.md) gap #5), so deleting the volume is the retention policy.
