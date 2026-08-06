# 00 — Overview

**Date:** 2026-08-05 · **Governed by:** [`STANDARDS.md`](../../STANDARDS.md)

Entry point for the spec set. Read this first, then the spec covering the area you are working on.

| Spec | Covers |
|---|---|
| [01 — Worker](01-worker.md) | Handler, pipeline, inference, the RunPod contract |
| [02 — Gateway core](02-gateway-core.md) | Domain model, `JobService`, protocols, persistence |
| [03 — Facades](03-facades.md) | Transports and what each costs the caller |
| [04 — Guardrails](04-guardrails.md) | Prompt and image safety hooks |
| [05 — Observability](05-observability.md) | Logging, correlation, health |
| [06 — Build & deploy](06-build-deploy.md) | Image, weights, CI, deploy, rollback |
| [07 — Testing](07-testing.md) | Strategy across both tiers |
| [08 — Production readiness](08-production-readiness.md) | What is deliberately not built, and what prod would cost |
| [09 — Benchmarks](09-benchmarks.md) | Measurement methodology and what gets reported |

> **Diagram:** [system context](https://excalidraw.com/#json=uZeWZkY3mvbnTlHe4SFAE,Jz6Rkp2yaUftbLUiqkydew) — opens in Excalidraw, editable

## Brief

Deploy a serverless endpoint on RunPod running an ML model, accepting a text prompt and returning a generated image. Deliverables: `handler.py`, a Docker image containing the handler **and the model**, a deployed endpoint, a demonstration, and documentation.

Graded on: following the setup instructions, handler/model integration, successful deployment, documentation clarity.

## Deliverable

What is actually handed over:

| Artefact | Role |
|---|---|
| Repository | Code, specs, and history. The history is itself evidence of how the decisions were reached |
| `README.md` | Entry point — what it is, how to run it, how to call it, measured results |
| **`BENCHMARKS.md`** | **The centrepiece.** Measured performance and cost, with methodology stated — see [09](09-benchmarks.md) |
| Generated images | Committed samples with their prompts and seeds, so any result is reproducible |
| Endpoint details | ID and a working request, so the reviewer can call it themselves |

`BENCHMARKS.md` carries the most weight. A working endpoint demonstrates the task was completed; a rigorous benchmark demonstrates the deployment was *understood* — where the time goes, what it costs, where it breaks, and which GPU is actually right rather than assumed.

## Scope

| Decision | Value |
|---|---|
| Model | `black-forest-labs/FLUX.1-dev`, bf16, unquantized |
| Inference | `diffusers.FluxPipeline`. No `torch.compile`, no ComfyUI |
| Weights | **Cached models.** RunPod pre-stages the repo on hosts. The baked image is also built, since the brief asks for an image containing the model |
| Build host | RunPod GPU Pod |
| Registry | GHCR |
| Tiers | Serverless worker (graded) + FastAPI gateway (beyond the brief) |
| Gateway hosting | **Local via `docker compose`.** Deployment path documented, not performed |
| Facade | **Async only** — submit, poll. No synchronous endpoint |
| Also built | API-key auth, idempotency keys, retry + circuit breaker, health endpoints, prompt guardrails, per-step progress |
| Documented only | Webhook-out, SSE, WebSocket, MCP, rate limiting, metrics export |
| Out of scope | Fine-tuning, LoRA, batch inference, multi-region |

## Why cached models, not weights in the image

The brief is explicit: *"Build a Docker image that includes your serverless handler and the model."*

The deployed endpoint nevertheless uses **cached models**: RunPod pre-stages the HuggingFace repository on host machines before a worker starts, preferring hosts that already hold it. Both images build from one Dockerfile via `BAKE_WEIGHTS`, and the baked one is published so the artefact the brief names demonstrably exists — but the cached endpoint serves traffic.

| | Baked (~45GB) | **Cached (~2.9GB)** |
|---|---|---|
| Fresh-worker scale-up | Pull 45GB | Pull 2.9GB; host already holds the model |
| Region | Any with capacity | Any with capacity |
| Build and push | 30-60 min, and may exceed registry layer caps | Minutes |
| Storage cost | Registry only | **None** |
| Weight transfer | Billed at build | **Unbilled, pre-staged** |
| Maturity | Stable | **Shipped 2026-08** — no beta label; two documented limits (one model per endpoint; all quantizations stage together) |

Stating the deviation rather than hiding it. Staging pulls the *whole* repository, so the ~24GB of duplicate single-file weights come along — unbilled and not our disk, but the reason scale-up is measured rather than assumed.

### The network volume was considered and dropped

An earlier revision deployed from a network volume. It was removed once cached models worked, because a volume's cost is a datacenter pin that narrows the GPU pool exactly when scaling up under load — the moment it was supposed to help — plus a per-GB monthly bill and a population step.

**A volume remains a valid option and no code carries it.** `weights.resolve()` already honours an explicit `WEIGHTS_PATH`, which is the same branch the baked image uses — so mounting a volume at that path would work without a line of change. What was removed is the config, the population procedure, and the volume plumbing in the deploy script: machinery for a mechanism nothing deploys.

It is not the *fallback*, because it would not be one. A volume is only a fallback if it is already populated, and populating it costs a Pod, a 33GB download, the storage bill and the datacenter pin — everything removing it avoided. The fallback is the baked image, already a build target.

Worth knowing: cached models mount at `/runpod-volume/huggingface-cache/hub`, the same path a network volume uses. Attaching both would put two mechanisms on one mount point.

The worker code is identical either way. `weights.resolve()` tries the configured path, then the model cache, so a deployment selects a mechanism by configuration alone — no code fork, and a volume would still work if anyone wanted one.


## Two design rules that everything else follows from

**1. `core/` depends on nothing outward.** It declares `Protocol` interfaces; `adapters/` implements them. Enforced by `import-linter`, not review. This is what makes each additional transport a small addition rather than a rewrite, and it is what lets the entire gateway be built and tested before a RunPod endpoint exists.

**2. No unit or integration test may require a GPU, weights, or an external network service.** This is a design constraint, not a testing preference — it forces the pipeline behind a lazily-initialised injectable accessor, which is the difference between a testable handler and an untestable one.

## Async as the default

Generation takes ~20-25s. A synchronous HTTP call held open that long fights every load balancer default and cannot be safely retried — a retry re-bills a fresh generation.

So `POST /v1/jobs` → `job_id`, `GET /v1/jobs/{id}` → result is the only interface. No synchronous endpoint is provided: it would bound concurrency by held connections rather than GPU capacity, and on a cold worker — 30-60s of pipeline load before 20-25s of generation — it would exceed any deadline safe against load-balancer idle timeouts. The convenience path would be the one most likely to look broken on a first call. `client/generate.py` covers the two-call ergonomics instead.

Async is the gateway's interface. It does not change what the **worker** returns: RunPod serverless has a fixed surface, `GET /status/{job_id}` returns the handler's output verbatim, and so the worker returns base64 by default ([01](01-worker.md)). Storage references are opt-in, because a key is only resolvable by a caller running the gateway — and the graded tier must not depend on the ungraded one.

## Sequencing

Phase 2b is the graded deliverable and is **blocked on RunPod credits**, which are pending an external reply. Everything else runs locally. The aim is to reduce 2b to pure execution — every artefact written and verified in advance — so it collapses into one sitting when credits land.

| Phase | Output | Gate | Blocked |
|---|---|---|---|
| 0 | Scaffold, both `pyproject.toml`, pre-commit, CI | `make check` green | No |
| 1 | Worker: schemas, pipeline protocol, inference, handler, guardrail, tests | Suite green, no GPU | No |
| 1b | **`README.md` and `client/generate.py`, written against the contract** | **A reviewer could run it the moment 2b lands** | No |
| 2a | `Dockerfile`, `fetch_weights.py`, runbook, benchmark harness | Weight filter verified via `list_repo_files`; harness runs against a fake | No |
| 2b | **Build and push, deploy the cached endpoint, smoke test** | **An image generated from a prompt** | **Credits** |
| 2c | Benchmark: latency sweeps, cold start, cost. Baked comparison if time allows | `BENCHMARKS.md` populated | **Credits** |
| 3 | Gateway core, adapters, migrations, auth, tests | Coverage gate | No |
| 4 | Async facade, reconciler, health, compose | E2E against a fake `RunPodClient` | No |
| 5 | Remaining docs, diagrams | Complete except measured numbers | Partly |

Splitting 2a from 2b is what makes the wait productive: it verifies both build-blockers in [06](06-build-deploy.md) with no GPU and no download.

2b ends the moment a prompt produces an image — that is the graded outcome, and nothing in 2c is allowed to delay reaching it. The baked endpoint and the variant comparison are the first things cut if time runs short.

Until 2b runs, `BENCHMARKS.md` does not exist and no figure anywhere is stated as measured.

## Risks

| Risk | Mitigation |
|---|---|
| **RunPod credits pending — materialised, not hypothetical** | Phases 0, 1, 1b, 2a proceed locally. **Cutoff: if no reply by end of 2026-08-05, self-fund ~$20 and proceed.** A $20 dependency must not be allowed to fail the only graded deliverable |
| HF licence gate blocks even the offline weight-filter check | Accept immediately; instant and independent of the credits reply |
| Gateway consumes time the graded deliverable needs | 2a completes before Phase 3 starts |
| ~45GB image push is slow and failure-prone | Build and push from the Pod; GHCR avoids Docker Hub pull limits |
| L40S unavailable in region | GPU fallback list; benchmark A100 80GB regardless |
| Model store is newly shipped (2026-08) | The baked image is the fallback and is already a build target; switching is a tag and a config change |
| `diffusers` API drift | Pin exact versions; `uv.lock` committed |

## Corrections to earlier design notes

1. **Weights are not in the deployed image.** Superseded twice: first by a network volume, then by cached models, which removed the volume's datacenter pin and storage bill. The baked image is still built and published because the brief names it — see *Why cached models* above.
2. **Cost figures were wrong by ~12×.** L40S serverless is $1.75/hr, not $0.54/hr — $0.99/hr is the *Pod* rate. 1024²/28 steps is ~20-25s, not 4-6s. Every figure is now measured or labelled an estimate.
3. **Module-level pipeline init replaced by a lazy accessor.** The original made `handler.py` unimportable without a GPU, therefore untestable.
4. **CI does not build the image.** Standard GitHub runners have ~14GB free disk.
5. **Gated-repo handling and the duplicate-weights trap added.** Both were absent; both block the build.
6. **Async promoted to the default facade**, which also resolves the payload-ceiling unknown.

Retained as correct: diffusers over ComfyUI, no `torch.compile`, `refresh_worker` on OOM, fail-fast startup validation, never deploying `latest`, and the webhook-plus-polling resilience argument.
