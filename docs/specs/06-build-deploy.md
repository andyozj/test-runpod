# 06 — Build & deploy

> **Diagram:** [image layers](https://excalidraw.com/#json=KhbTALYghWAfdJV8d5Wcs,9OZHA523fBKl-aOIJQc6Ow) — opens in Excalidraw, editable

## Two build-blockers

Both are non-obvious, both stop the build dead, and both are verifiable **before** credits arrive.

### 1. Gated repository

FLUX.1-dev requires accepting the licence on HuggingFace and an `HF_TOKEN`. Passed as a BuildKit secret — never `ARG`, never `ENV`, never a `COPY`'d file. An `ARG`-passed token is recoverable from image history by anyone who pulls the image.

```dockerfile
RUN --mount=type=secret,id=hf_token \
    HF_TOKEN=$(cat /run/secrets/hf_token) python scripts/fetch_weights.py
```

### 2. Duplicate weights

The repository ships the `diffusers` sharded layout **and** standalone `flux1-dev.safetensors` (23.8GB) plus `ae.safetensors`. A naive `snapshot_download` pulls both — roughly 56GB instead of 33GB, for weights that are already there in another format.

`fetch_weights.py` uses `ignore_patterns` to take only the diffusers layout.

**Verified offline in Phase 2a.** `HfApi().list_repo_files()` returns the file list without downloading anything, so the filter is asserted against the real manifest for the cost of one API call. Discovering this on a running Pod costs an hour and a re-push.

## Revision pinning

FLUX.1-dev has no `stable` tag — HuggingFace offers `main` and commit SHAs. Building against `main` means two builds a week apart can differ silently while both claim to be the same model.

So the SHA is resolved once and committed:

```
contracts/model-revision.txt      # e.g. 0ef5fff789c832c5c7f4e127f94c8b54bbcced44
```

`fetch_weights.py --revision $(cat contracts/model-revision.txt)` for every build. Updating the model is a commit that changes one file and is visible in review, rather than a silent consequence of building on a different day.

This is what makes `model_version` in the worker's response ([01](01-worker.md)) a fact rather than a label.

### The cache must prove what it holds

A baked image knows its weights — it downloaded them during its own build. A cache could hold anything: staged weeks earlier, possibly from a different revision.

`weights.resolve()` starts only if the pinned revision from `contracts/model-revision.txt` is present, and the error names what it found instead. RunPod's own example sorts available snapshots and takes the first, which would run a model the response then reports as the pinned revision — misattributing every image.


## Image tags

```
ghcr.io/<owner>/flux-worker:{version}-{short-sha}-{variant}

ghcr.io/<owner>/flux-worker:0.1.0-a3f21c8-baked
ghcr.io/<owner>/flux-worker:0.1.0-a3f21c8-slim
```

Version for humans, short SHA so two builds of one version are distinguishable, variant because both must coexist in the registry and be deployed to separate endpoints.

The `{version}-{short-sha}` prefix is the same string `/health` reports ([05](05-observability.md)), so a running endpoint identifies its own image without cross-referencing anything.

Immutable. Never `latest` ([`STANDARDS.md`](../../STANDARDS.md) §11).

## Layer order

Ordered by rate of change, slowest first:

| Layer | Size | Changes |
|---|---|---|
| System deps — CUDA runtime, Python | ~2GB | Almost never |
| Python deps — torch, diffusers, transformers | ~7GB | Rarely |
| Model weights — diffusers layout | ~33GB | Never |
| Application code | ~1MB | Every commit |

Weights sit **below** application code. Reversing those two means every code change invalidates 33GB and costs an hour per build.

Base: `nvidia/cuda:12.4.1-runtime-ubuntu22.04`. `runtime` not `devel` — the devel image adds several GB of compiler toolchain that nothing at inference time uses.

Expected final size ~45GB.

## Two weight-delivery variants

Both are built from one image definition. The worker code is identical; `weights.resolve()` tries the configured path, then the model cache, so a deployment picks a mechanism by configuration alone.

**Baked.** `fetch_weights.py` runs at build time, weights land in the image, `WEIGHTS_PATH` points at the image path. ~45GB, no region constraint. Built and published because the brief names it; not the deployed variant.

**Cached models (deployed).** A ~2.9GB image with no weights layer, with `WEIGHTS_PATH` deliberately unset. RunPod pre-stages the repository on host machines and mounts the HuggingFace cache; the worker resolves `models--{org}--{name}/snapshots/{revision}` beneath `MODEL_CACHE_ROOT`. Enabled by the endpoint's **Model** field plus an HF token, since FLUX.1-dev is gated. No code change, no third image, no datacenter pin, no storage bill.

**The resolver refuses a revision mismatch.** RunPod's own example sorts the available snapshots and takes the first, which would run a model the response then reports as the pinned revision — misattributing every image and silently invalidating any comparison between endpoints. Ours starts only if the pinned revision is present, and names what it found instead.

Beta. The fallback is the baked image, already a build target — a tag and a config change, no new infrastructure.

```dockerfile
ARG BAKE_WEIGHTS=true
RUN --mount=type=secret,id=hf_token \
    if [ "$BAKE_WEIGHTS" = "true" ]; then \
      HF_TOKEN=$(cat /run/secrets/hf_token) python scripts/fetch_weights.py; \
    fi
```

Two consequences worth stating rather than discovering:

- **The volume pins the endpoint to one datacenter.** Volume and endpoint must be co-located, which narrows the GPU pool — and it narrows exactly when you are scaling up under load, which is when the volume was supposed to help.
- **The startup check must know which variant it is.** Fail fast if `WEIGHTS_PATH` is absent. Without it, a misconfigured volume mount silently falls through to downloading 33GB from HuggingFace on every cold start, which looks like "slow" rather than "broken".

Populating the volume is a one-time Pod operation, documented in `RUNBOOK.md`. Storage bills per GB per month for as long as it exists — delete it after the benchmark run.

## Registry

GHCR, not Docker Hub. Docker Hub's free-tier pull rate limits apply to the *puller*, and RunPod scaling up several workers means several pulls of a 45GB image in a short window — exactly the shape that trips the limit, at exactly the moment you need capacity.

Tags are immutable and version-pinned. `latest` is never deployed to an endpoint ([`STANDARDS.md`](../../STANDARDS.md) §11).

## Building on a Pod

The build does not happen locally. A ~45GB `linux/amd64` build under emulation on an arm64 Mac, followed by a 45GB push over home broadband, is hours of wall-clock and a fragile upload.

A RunPod GPU Pod has datacenter bandwidth to both HuggingFace and GHCR, and lets the handler be smoke-tested on a real GPU before the serverless endpoint is created — which separates "the image is broken" from "the endpoint is misconfigured" while they are still cheap to tell apart.

Procedure lives in `RUNBOOK.md`. Summary: provision pod → clone → `docker buildx build` with the HF secret → smoke test locally on the pod → push to GHCR → create endpoint against the tag → terminate the pod.

## Deploy and rollback

Deploy: build and push on the Pod → run the deploy workflow with the new tag → `saveEndpoint` updates the endpoint → workers pick it up on next cold start; FlashBoot workers drain naturally.

Rollback: the same workflow with the previous tag. This works only because tags are immutable — with `latest`, the previous image no longer exists to roll back to.

The prior known-good tag is recorded in the runbook at each deploy. A rollback procedure that begins with "work out which tag was good" is not a rollback procedure.

## Gateway deployment

The gateway runs **locally via `docker compose`**. It is not hosted.

```bash
cp .env.example .env          # RUNPOD_API_KEY, RUNPOD_ENDPOINT_ID, GATEWAY_API_KEY
docker compose up             # gateway + postgres; migrations run on boot
```

The graded artefact — the RunPod endpoint — is a public URL and callable regardless, so hosting the gateway adds nothing to what can be demonstrated. It would add a live liability: a credential-backed GPU spender exposed continuously, on free credits, with no per-key quota. Rate limiting is the top item in [08](08-production-readiness.md); hosting this publicly before building it would be putting the exposure ahead of the control.

Compose must work on a clean clone with two environment variables and one command. A local setup that needs a README paragraph of troubleshooting is worse than no local setup.

The production path is documented rather than performed: container image, managed Postgres, secrets from the platform's store, the reconciler as a separate process so gateway replicas do not each poll, and the shared circuit-breaker state that becomes necessary the moment there is more than one replica.


## Endpoint configuration as code

RunPod's REST API at `https://rest.runpod.io/v1` is the current management surface; GraphQL `saveEndpoint` is legacy and models the problem differently.

REST splits it in two, and the split shapes the script:

| Resource | Owns |
|---|---|
| **Template** | Image, environment variables, container disk |
| **Endpoint** | GPU list, scaling, timeouts, network volume, CUDA filter, `templateId` |

So a deploy is two idempotent upserts — look up by name, create if absent, `PATCH` if present — not one mutation.

So the endpoints are **not** created by clicking through the console. Both are declared in committed config and applied by a script:

```
deploy/endpoints/baked.yaml
scripts/apply_endpoint.py --config deploy/endpoints/baked.yaml --tag 0.1.0-a3f21c8-baked
```

Each file carries the GPU priority list, min and max workers, idle timeout, execution timeout, scaler type and value, network volume, datacenters, environment, and `allowed_cuda_versions`.

**`allowed_cuda_versions` is the lever for the wheel/driver pairing.** `uv.lock` resolves torch with cu130 wheels, which need CUDA-13-capable hosts. Declaring the filter makes a mismatch fail at *scheduling* — no worker is offered — rather than at model load on a worker already billing. It is also the one-line fix if the first deploy lands on older drivers.

The console is not the source of truth. "Why is the idle timeout 60s?" must be answerable from the repository and reviewable in a diff, and an endpoint deleted by accident must be reconstructible without archaeology. Recording values in a runbook would achieve the first two; only applied config achieves the third.

## Secrets

RunPod provides a secrets manager. Secrets are stored encrypted and referenced from a template's environment section:

```
HF_TOKEN={{ RUNPOD_SECRET_huggingface_token }}
S3_ACCESS_KEY={{ RUNPOD_SECRET_s3_access_key }}
S3_SECRET_KEY={{ RUNPOD_SECRET_s3_secret_key }}
```

The value is substituted at container start. This matters for a reason beyond storage: the endpoint config above is **committed to the repository**, so it must contain references rather than values. Without the secrets manager, config-as-code and secret hygiene would be in direct conflict — one of them would have to give.

`HF_TOKEN` is still needed at *build* time as a BuildKit secret; the RunPod secret covers runtime only.

## CI/CD

Split by what each stage actually needs.

| Stage | Runs on | Why |
|---|---|---|
| Lint, types, `import-linter`, tests, contract conformance | GitHub-hosted | No GPU, no weights, no large disk |
| Gateway image build | GitHub-hosted | Small image |
| **Worker image build and push** | **RunPod Pod** | ~45GB image against ~14GB of runner disk. A hard stop, not a slow path |
| **Endpoint deploy** | **GitHub-hosted** | It is an API call. No disk, no image, no GPU |

The insight worth stating: **build and deploy have completely different requirements.** Build needs datacenter bandwidth and tens of gigabytes of disk. Deploy needs one authenticated mutation. Coupling them would push the whole pipeline onto expensive infrastructure for the sake of its cheapest step.

So the deploy workflow is manually triggered, takes a tag as input, and calls `apply_endpoint.py`. Rollback is the same workflow with the previous tag — which works only because tags are immutable.

A self-hosted GitHub Actions runner on a RunPod Pod would let the build move into CI as well. Not done: it means keeping a Pod alive for a build that happens a handful of times, and the runbook procedure is adequate at this frequency.

## Configuration

Runtime configuration reaches the endpoint through RunPod's environment settings, never baked into the image: `RUNPOD_API_KEY` is not needed by the worker, but S3 credentials, `WEIGHTS_PATH`, guardrail settings, and log level are.

`.env.example` is committed listing every variable with a description and no value. `.env` is git-ignored.

## Phase 2a checklist

Everything below is doable now, with no credits and no GPU:

- [ ] `fetch_weights.py` written; `ignore_patterns` asserted against `list_repo_files()`
- [ ] `HF_TOKEN` verified against the gated repo
- [ ] Model revision resolved and committed to `contracts/model-revision.txt`
- [ ] `Dockerfile` written, layer order correct, secret mount correct, `BAKE_WEIGHTS` arg working
- [ ] `.dockerignore` excludes `tests/`, `docs/`, `.git`, `.venv`, `__pycache__`
- [ ] Volume manifest format defined; startup revision check written and unit-tested
- [ ] Storage client written against a fake S3; datacenter chosen from the five that support the S3 API
- [ ] Gateway image-proxy route written and tested against a fake storage backend
- [ ] Benchmark harness written and running against a fake pipeline
- [ ] `RUNBOOK.md` written end to end, including volume population and both endpoint configurations
- [ ] Endpoint configuration recorded as values, ready to enter

When credits land, 2b is execution with no decisions left in it.
