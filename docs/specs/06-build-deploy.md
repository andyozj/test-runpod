# 06 — Build & deploy

> **Diagram:** image layers — *pending Excalidraw*

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

Both are built. The worker code is identical; only the weight path differs, so this is one image definition with a build argument rather than two codebases.

**Baked (primary).** `fetch_weights.py` runs at build time, weights land in the image, `WEIGHTS_PATH` points at the image path. ~45GB. No region constraint. This is what the brief asks for.

**Network volume.** The weights layer is skipped, producing a ~10GB image. A RunPod network volume is populated once from a Pod and mounted at `/runpod-volume`; `WEIGHTS_PATH` points there.

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

## CI

CI runs lint, types, `import-linter`, and non-GPU tests. **It does not build the worker image.** Standard GitHub runners have ~14GB free disk against a ~45GB image; this is a hard stop, not a slow path, and it is recorded so nobody spends an afternoon rediscovering it.

The gateway image is small and *is* built in CI.

## Deploy and rollback

Deploy: build → push versioned tag → update the endpoint's image tag → workers pick it up on next cold start; FlashBoot workers drain naturally.

Rollback: repoint the endpoint at the previous tag. This works only because tags are immutable — with `latest`, the previous image no longer exists to roll back to.

The prior known-good tag is recorded in the runbook at each deploy. A rollback procedure that begins with "work out which tag was good" is not a rollback procedure.

## Configuration

Runtime configuration reaches the endpoint through RunPod's environment settings, never baked into the image: `RUNPOD_API_KEY` is not needed by the worker, but bucket credentials, guardrail settings, and log level are.

`.env.example` is committed listing every variable with a description and no value. `.env` is git-ignored.

## Phase 2a checklist

Everything below is doable now, with no credits and no GPU:

- [ ] `fetch_weights.py` written; `ignore_patterns` asserted against `list_repo_files()`
- [ ] `HF_TOKEN` verified against the gated repo
- [ ] `Dockerfile` written, layer order correct, secret mount correct
- [ ] `.dockerignore` excludes tests, docs, `.git`
- [ ] Benchmark harness written and running against a fake pipeline
- [ ] `RUNBOOK.md` written end to end
- [ ] Endpoint configuration recorded as values, ready to enter

When credits land, 2b is execution with no decisions left in it.
