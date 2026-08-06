# 06 Build & deploy

> **Diagram:** [image layers](https://excalidraw.com/#json=KhbTALYghWAfdJV8d5Wcs,9OZHA523fBKl-aOIJQc6Ow) (opens in Excalidraw, editable)

## Two build-blockers

Both are non-obvious, both stop the build dead, and both are verifiable **before** credits arrive.

### 1. Gated repository

FLUX.1-dev requires accepting the licence on HuggingFace and an `HF_TOKEN`. Passed as a BuildKit secret: never `ARG`, never `ENV`, never a `COPY`'d file. An `ARG`-passed token is recoverable from image history by anyone who pulls the image.

```dockerfile
RUN --mount=type=secret,id=hf_token \
    HF_TOKEN=$(cat /run/secrets/hf_token) python scripts/fetch_weights.py
```

### 2. Duplicate weights

The repository ships the `diffusers` sharded layout **and** standalone `flux1-dev.safetensors` (23.8GB) plus `ae.safetensors`. A naive `snapshot_download` pulls both: roughly 56GB instead of 33GB, for weights already there in another format.

`fetch_weights.py` uses `ignore_patterns` to take only the diffusers layout.

**Verified offline in Phase 2a.** `HfApi().list_repo_files()` returns the file list without downloading anything, so the filter is asserted against the real manifest for the cost of one API call. Discovering this on a running Pod costs an hour and a re-push.

## Revision: discovered, not pinned

FLUX.1-dev has no `stable` tag: HuggingFace offers `main` and commit SHAs. The original design pinned a SHA in `contracts/model-revision.txt` and built against it.

**That file no longer exists.** The deployed endpoint uses cached models, which RunPod stages from the console's Model field, a field with no revision control. A pin the platform cannot honour is a pin that only ever produces a startup refusal, so the worker reports the revision it actually loaded instead of refusing over a value nobody can set.

Discovery, in `worker/weights.py`:

| Layout | Evidence used |
|---|---|
| Cache snapshot (`.../snapshots/<sha>`) | The directory name is the commit SHA |
| Baked image | `MANIFEST.json`, written by `fetch_weights.py` at build time |
| Anything else | None; `MODEL_REVISION` in the environment supplies it, defaulting to `unknown` |

`model_version` on every response is `<model_id>@<discovered revision>`: a reported fact, not a label and not a promise.

### The cache must still prove what it holds

A baked image knows its weights: it downloaded them during its own build. A cache could hold anything, staged weeks earlier.

`weights.resolve()` identifies the staged snapshot through the cache's `refs/main`, or by it being the only snapshot present. The one case that still refuses to start is several snapshots with no ref naming the staged one: RunPod's own example sorts and takes the first, which would run a model the response then misattributes on every image.


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

| Layer | Share of the image | Changes |
|---|---|---|
| Base (`ubuntu:22.04`) + `ca-certificates` + `uv` + Python 3.11 | Small | Almost never |
| Python deps: torch, diffusers, transformers | **Nearly all of it** | Rarely |
| Model weights: diffusers layout (**baked variant only**) | ~33GB, the repo's own size | Never |
| `contracts/`, `scripts/`, application code | Tens of kB | Every commit |

Weights sit **below** application code. Reversing those two means every code change invalidates 33GB and costs an hour per build.

No per-layer byte counts here on purpose. `docker history` reports build-time layer sizes that do not reconcile with the finished image (5.4GB of layers against a 2.92GB image, once deduplication and the discarded install cache are accounted for), so quoting both invites a reader to add up one column and check it against the other. The one number worth trusting is the whole-image measurement.

**Measured total, deployed slim variant: 2.92GB** (`docker image inspect --format '{{.Size}}'` → 2,916,120,790 bytes, 13 layers). The baked variant is ~45GB and is not published.

Base: **`ubuntu:22.04`**, not a CUDA image. The torch wheel ships its own CUDA libraries, so `nvidia/cuda:*-runtime` duplicated ~6.6GB that nothing ever loads: 11.9GB → 2.92GB. `NVIDIA_VISIBLE_DEVICES`/`NVIDIA_DRIVER_CAPABILITIES` are set explicitly, because the CUDA base image was what used to set them.

## Two weight-delivery variants

Both are built from one image definition. The worker code is identical; `weights.resolve()` tries the configured path, then the model cache, so a deployment picks a mechanism by configuration alone.

The decision rule, stated once: **cached models when the weights are on HuggingFace** (they are, here), **network volume when they are not** (cached models stage from HF only, so a volume stops being a choice and becomes the mechanism), **baked never in production**. The baked image exists because the brief asks for an image that includes the model, and because it is the one fallback that can be built *before* an incident rather than populated during one.

**Baked.** `fetch_weights.py` runs at build time, weights land in the image, `WEIGHTS_PATH` points at the image path. ~45GB, no region constraint. A build target (`make build-baked`) because the brief names it; neither published nor deployed.

**Cached models (deployed).** A ~2.9GB image with no weights layer, with `WEIGHTS_PATH` deliberately unset. RunPod pre-stages the repository on host machines and mounts the HuggingFace cache; the worker resolves `models--{org}--{name}/snapshots/{revision}` beneath `MODEL_CACHE_ROOT`. Enabled by the endpoint's **Model** field plus an HF token, since FLUX.1-dev is gated. No code change, no third image, no datacenter pin, no storage bill.

**The resolver refuses to guess between snapshots.** RunPod's own example sorts the available snapshots and takes the first, which would run a model the response then misattributes, silently invalidating any comparison between endpoints. Ours takes the one named by `refs/main`, or the only one present, and otherwise refuses to start and names what it found. It does not compare against a pin; there is none (see *Revision: discovered, not pinned*).

Recently shipped: RunPod's "model store", engineering write-up 2026-08-04; the docs carry no beta label. Its real caveats: one cached model per endpoint, whole-repo staging (all quantizations), and **console-only configuration** (the Model field has no REST API surface yet). The fallback is the baked image, already a build target: a tag and a config change, no new infrastructure.

```dockerfile
ARG BAKE_WEIGHTS=true
RUN --mount=type=secret,id=hf_token \
    if [ "$BAKE_WEIGHTS" = "true" ]; then \
      HF_TOKEN=$(cat /run/secrets/hf_token) python scripts/fetch_weights.py; \
    fi
```

**The network-volume variant below was tried and dropped.** It is kept here as the reasoning that produced the decision; nothing in the repo builds or configures it, and the fallback for cached models is the baked image, not a volume. A volume is only a fallback once it is *already populated*, and populating one costs everything removing it avoided.

Two consequences, which is what settled it:

- **The volume pins the endpoint to one datacenter.** Volume and endpoint must be co-located, which narrows the GPU pool, and it narrows exactly when you are scaling up under load, which is when the volume was supposed to help.
- **The startup check must know which variant it is.** Fail fast if `WEIGHTS_PATH` is absent. Without it, a misconfigured volume mount silently falls through to downloading 33GB from HuggingFace on every cold start, which looks like "slow" rather than "broken".

Populating the volume would have been a one-time Pod operation, and storage bills per GB per month for as long as the volume exists. That population cost is what settled the decision: the runbook documents no volume procedure, because none was kept.

## Registry

GHCR, not Docker Hub. Docker Hub's free-tier pull rate limits apply to the *puller*, and RunPod scaling up several workers means several pulls of a 45GB image in a short window: exactly the shape that trips the limit, at exactly the moment you need capacity.

Tags are immutable and version-pinned. `latest` is never deployed to an endpoint ([`STANDARDS.md`](../../STANDARDS.md) §11).

## Where the build happens

**Locally, or in CI. No Pod.** That changed when the deployed image stopped carrying weights: a Pod was only ever a build machine for the ~45GB baked image, where a `linux/amd64` build under emulation on an arm64 Mac plus a 45GB push over home broadband was hours of wall-clock and a fragile upload. At 2.92GB neither problem exists.

Two paths, same Dockerfile:

- `make build-slim` on a laptop, then `docker push`. `--platform linux/amd64` is set in the Makefile; without it the arm64 result starts and immediately dies on RunPod.
- `.github/workflows/deploy.yml`, which builds and pushes on a standard GitHub runner. The slim image fits the runner's ~14GB disk; the baked one would not, which is why only the slim variant is built in CI.

Procedure lives in `RUNBOOK.md`. The baked variant, if it is ever needed, is `make build-baked` and still wants a machine with datacenter bandwidth and tens of gigabytes of free disk.

## Deploy and rollback

Deploy: build and push locally → `apply_endpoint.py` with the new tag, which bounces `workersMax` 0 → configured automatically. Rolling releases are documented for API and console updates alike: idle workers replaced immediately, busy ones after their job, and old versions serve "until they are replaced". Gradual by design, no stated bound. The corner the docs do not name: a **FlashBoot-retained** worker (scaled to zero, unbilled) is neither idle nor processing, and one resumed the previous image on its next job (observed 2026-08-06). The bounce is the docs' own remedy for strict consistency; the script just automates it. Two earlier revisions of this paragraph claimed more than the evidence, first "drains naturally", then "the docs are silent", and each was corrected when tested.

Rollback: the same workflow with the previous tag. Works only because tags are immutable; with `latest`, the previous image no longer exists to roll back to.

The prior known-good tag is recorded in the runbook at each deploy. A rollback procedure that begins with "work out which tag was good" is not a rollback procedure.

## Gateway deployment

The gateway runs **locally via `docker compose`**. It is not hosted.

```bash
cp .env.example .env          # RUNPOD_API_KEY, RUNPOD_ENDPOINT_ID, GATEWAY_API_KEYS
docker compose up             # the gateway alone
```

**No Postgres service and no migrations.** Persistence is `InMemoryJobRepository` behind the same protocol, so starting a database nothing connects to would be scenery, not infrastructure. Jobs do not survive a restart. `compose.yaml` supplies `GATEWAY_API_KEYS=demo:local-development-key` only when the variable is unset; the application has no built-in credential and fails startup without one.

The graded artefact (the RunPod endpoint) is a public URL and callable regardless, so hosting the gateway adds nothing to what can be demonstrated. It would add a live liability: a credential-backed GPU spender exposed continuously, on free credits, with no per-key quota. Rate limiting is the top item in [08](08-production-readiness.md); hosting this publicly before building it would put the exposure ahead of the control.

Compose must work on a clean clone with two environment variables and one command. A local setup that needs a README paragraph of troubleshooting is worse than no local setup.

The production path is documented rather than performed: container image, managed Postgres, secrets from the platform's store, the reconciler as a separate process so gateway replicas do not each poll, and the shared circuit-breaker state that becomes necessary the moment there is more than one replica.


## Endpoint configuration as code

RunPod's REST API at `https://rest.runpod.io/v1` is the current management surface; GraphQL `saveEndpoint` is legacy and models the problem differently.

REST splits it in two, and the split shapes the script:

| Resource | Owns |
|---|---|
| **Template** | Image, environment variables, container disk |
| **Endpoint** | GPU list, scaling, timeouts, network volume, CUDA filter, `templateId` |

A deploy is two idempotent upserts (look up by name, create if absent, `PATCH` if present), not one mutation.

The endpoints are **not** created by clicking through the console. Both are declared in committed config and applied by a script:

```
deploy/endpoints/baked.yaml
scripts/apply_endpoint.py --config deploy/endpoints/baked.yaml --tag 0.1.0-a3f21c8-baked
```

Each file carries the GPU priority list, min and max workers, idle timeout, execution timeout, scaler type and value, network volume, datacenters, environment, and `allowed_cuda_versions`.

**`allowed_cuda_versions` is the lever for the wheel/driver pairing.** `uv.lock` resolves torch with cu130 wheels, which need CUDA-13-capable hosts. Declaring the filter makes a mismatch fail at *scheduling* (no worker is offered) rather than at model load on a worker already billing. It is also the one-line fix if the first deploy lands on older drivers.

The console is not the source of truth. "Why is the idle timeout 60s?" must be answerable from the repository and reviewable in a diff, and an endpoint deleted by accident must be reconstructible without archaeology. Recording values in a runbook would achieve the first two; only applied config achieves the third.

One documented exception: the cached-model **Model field is console-only**. The REST API exposes no model field (verified against its OpenAPI, 2026-08-06). It is recorded in the runbook's deploy procedure as the single manual step, and this paragraph is the reminder to fold it into `apply_endpoint.py` the day the API grows it.

## Secrets

RunPod provides a secrets manager. Secrets are stored encrypted and referenced from a template's environment section:

```
HF_TOKEN={{ RUNPOD_SECRET_huggingface_token }}
S3_ACCESS_KEY={{ RUNPOD_SECRET_s3_access_key }}
S3_SECRET_KEY={{ RUNPOD_SECRET_s3_secret_key }}
```

The value is substituted at container start. This matters beyond storage: the endpoint config above is **committed to the repository**, so it must contain references rather than values. Without the secrets manager, config-as-code and secret hygiene would be in direct conflict; one of them would have to give.

`HF_TOKEN` is still needed at *build* time as a BuildKit secret; the RunPod secret covers runtime only.

## CI/CD

Split by what each stage actually needs.

| Stage | Runs on | Why |
|---|---|---|
| Lint, types, `import-linter`, tests, contract conformance | GitHub-hosted | No GPU, no weights, no large disk |
| Gateway image build | Not built in CI | Only `compose.yaml` builds it, locally |
| **Slim worker image build and push** | **GitHub-hosted** | 2.92GB fits the runner's ~14GB disk |
| **Baked worker image build** | **Not in CI** | ~45GB against ~14GB of runner disk. A hard stop, not a slow path. `make build-baked`, on a machine that can hold it |
| **Endpoint deploy** | **GitHub-hosted** | It is an API call. No disk, no image, no GPU |

The original plan put the build on a RunPod Pod, because the only image then was the ~45GB baked one. Dropping weights from the deployed image removed the constraint and the Pod with it.

`.github/workflows/deploy.yml`, in order: `checks` (the whole of `ci.yml`, on the tag-push entry point too) → `build-push` → `verify-tag` (`docker manifest inspect` on the tag about to be applied, so a rollback to a tag that was never pushed fails the workflow instead of the endpoint) → `deploy` (`apply_endpoint.py`) → `smoke-test` (`pytest -m gpu tests/e2e` against the live endpoint).

Rollback is the same workflow with the previous tag, which works only because tags are immutable. `smoke-test` is **detection, not prevention**: `deploy` has already mutated the endpoint before it runs, so a failure there means a manual rollback, not an automatic one. Making it prevention needs a second endpoint and a traffic switch, which the platform's endpoint model does not give for free.

## Configuration

Runtime configuration reaches the endpoint through RunPod's environment settings, never baked into the image: `RUNPOD_API_KEY` is not needed by the worker, but S3 credentials, `WEIGHTS_PATH`, guardrail settings, and log level are.

`.env.example` is committed listing every variable with a description and no value. `.env` is git-ignored.

## Phase 2a checklist

The original Phase 2a plan, kept as the record of what was scoped before credits arrived. Three items were later dropped rather than done: the volume manifest (the volume variant was dropped), the S3 storage client and the gateway image-proxy route (object storage is not built; [08](08-production-readiness.md) gap #10).



- [ ] `fetch_weights.py` written; `ignore_patterns` asserted against `list_repo_files()`
- [ ] `HF_TOKEN` verified against the gated repo
- [ ] Revision discovery written and unit-tested (cache `refs/main`, sole snapshot, baked `MANIFEST.json`, ambiguous-snapshot refusal)
- [ ] `Dockerfile` written, layer order correct, secret mount correct, `BAKE_WEIGHTS` arg working
- [ ] `.dockerignore` excludes `tests/`, `docs/`, `.git`, `.venv`, `__pycache__`
- [ ] Volume manifest format defined; startup revision check written and unit-tested
- [ ] Storage client written against a fake S3; datacenter chosen from the five that support the S3 API
- [ ] Gateway image-proxy route written and tested against a fake storage backend
- [ ] Benchmark harness written and running against a fake pipeline
- [ ] `RUNBOOK.md` written end to end, including volume population and both endpoint configurations
- [ ] Endpoint configuration recorded as values, ready to enter

When credits land, 2b is execution with no decisions left in it.
