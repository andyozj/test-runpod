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

> **Diagram:** system context — *pending Excalidraw*

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
| Weights | **Two variants.** Baked into the image (primary, per the explicit instruction) and a network-volume variant, benchmarked against each other |
| Build host | RunPod GPU Pod |
| Registry | GHCR |
| Tiers | Serverless worker (graded) + FastAPI gateway (beyond the brief) |
| Primary facade | **Async** — submit, poll |
| Also built | Sync wrapper (demo-grade), API-key auth, retry + circuit breaker, health endpoints, prompt guardrails |
| Documented only | Webhook-out, SSE, WebSocket, MCP, rate limiting, metrics export |
| Out of scope | Fine-tuning, LoRA, batch inference, multi-region |

## Why two weight-delivery variants

The brief is explicit: *"Build a Docker image that includes your serverless handler and the model."* Baked weights are therefore the primary deliverable, and a volume-only submission would fail the first grading criterion.

But network volumes are one of RunPod's distinguishing features, and ignoring them leaves the platform half-used. So both are built and measured, and the report says when each wins.

| | Baked | Network volume |
|---|---|---|
| Image size | ~45GB | ~10GB |
| Fresh-worker scale-up | Pull 45GB | Pull 10GB, mount weights |
| Warm worker | Image layer cached, disk → VRAM | Volume read → VRAM |
| Region | Any datacenter | **Pinned to the volume's datacenter** |
| Build/push iteration | Slow | Fast |
| Storage cost | None beyond registry | Per-GB, per-month |

The region constraint is the tradeoff that usually goes unwritten: a volume pins the endpoint to one datacenter, which narrows the GPU pool available — and that bites precisely when scaling up under load, which is the moment the volume was supposed to help.

The hypothesis worth testing is that baked wins on availability and steady state, while the volume wins on scale-up latency and on iteration speed during development. [09](09-benchmarks.md) measures it rather than assuming it.

The worker code is identical across both. Only the weight path differs, resolved from settings, so this is a deployment variant rather than a code fork.

## Two design rules that everything else follows from

**1. `core/` depends on nothing outward.** It declares `Protocol` interfaces; `adapters/` implements them. Enforced by `import-linter`, not review. This is what makes each additional transport a small addition rather than a rewrite, and it is what lets the entire gateway be built and tested before a RunPod endpoint exists.

**2. No unit or integration test may require a GPU, weights, or an external network service.** This is a design constraint, not a testing preference — it forces the pipeline behind a lazily-initialised injectable accessor, which is the difference between a testable handler and an untestable one.

## Async as the default

Generation takes ~20-25s. A synchronous HTTP call held open that long fights every load balancer default and cannot be safely retried — a retry re-bills a fresh generation.

So `POST /v1/jobs` → `job_id`, `GET /v1/jobs/{id}` → result is the primary interface. `POST /v1/images` exists as a thin blocking wrapper for `curl` and the demo, documented as demo-grade.

This also removes a risk from the build: with async as the default the worker uploads to object storage and returns a **reference** rather than inline base64, so RunPod's undocumented response-payload ceiling stops being something we discover the hard way. Base64 remains as the zero-infrastructure fallback.

## Sequencing

Phase 2b is the graded deliverable and is **blocked on RunPod credits**, which are pending an external reply. Everything else runs locally. The aim is to reduce 2b to pure execution — every artefact written and verified in advance — so it collapses into one sitting when credits land.

| Phase | Output | Gate | Blocked |
|---|---|---|---|
| 0 | Scaffold, both `pyproject.toml`, pre-commit, CI | `make check` green | No |
| 1 | Worker: schemas, pipeline protocol, inference, handler, guardrail, tests | Suite green, no GPU | No |
| 2a | `Dockerfile`, `fetch_weights.py`, runbook, benchmark harness | Weight filter verified via `list_repo_files`; harness runs against a fake | No |
| 2b | **Build both variants on Pod, push, populate volume, deploy two endpoints, smoke test, benchmark** | **Image generated; `BENCHMARKS.md` populated incl. baked-vs-volume** | **Credits** |
| 3 | Gateway core, adapters, migrations, auth, tests | Coverage gate | No |
| 4 | Async + sync facades, reconciler, health, compose | E2E against a fake `RunPodClient` | No |
| 5 | Client demo, README, docs | Complete except measured numbers | Partly |

Splitting 2a from 2b is what makes the wait productive: it verifies both build-blockers in [06](06-build-deploy.md) with no GPU and no download.

Until 2b runs, `BENCHMARKS.md` does not exist and no figure anywhere is stated as measured.

## Risks

| Risk | Mitigation |
|---|---|
| **RunPod credits pending — materialised, not hypothetical** | Phases 0, 1, 2a, 3, 4 proceed locally; 2b reduced to execution |
| HF licence gate blocks even the offline weight-filter check | Accept immediately; instant and independent of the credits reply |
| Gateway consumes time the graded deliverable needs | 2a completes before Phase 3 starts |
| ~45GB image push is slow and failure-prone | Build and push from the Pod; GHCR avoids Docker Hub pull limits |
| L40S unavailable in region | GPU fallback list; benchmark A100 80GB regardless |
| **Volume variant pins the endpoint to one datacenter**, narrowing the GPU pool | Baked variant is the primary deliverable and has no region constraint; the volume endpoint is the comparison, not the fallback |
| Volume variant doubles the 2b deploy work | Worker code is identical; only the weight path differs. If time runs short, the baked variant alone still satisfies the brief |
| `diffusers` API drift | Pin exact versions; `uv.lock` committed |

## Corrections to earlier design notes

1. **Weights baked into the image as the primary variant.** The brief requires it. Superseded in part: rather than only documenting the network-volume alternative, both are built and benchmarked — see *Why two weight-delivery variants* above.
2. **Cost figures were wrong by ~12×.** L40S serverless is $1.75/hr, not $0.54/hr — $0.99/hr is the *Pod* rate. 1024²/28 steps is ~20-25s, not 4-6s. Every figure is now measured or labelled an estimate.
3. **Module-level pipeline init replaced by a lazy accessor.** The original made `handler.py` unimportable without a GPU, therefore untestable.
4. **CI does not build the image.** Standard GitHub runners have ~14GB free disk.
5. **Gated-repo handling and the duplicate-weights trap added.** Both were absent; both block the build.
6. **Async promoted to the default facade**, which also resolves the payload-ceiling unknown.

Retained as correct: diffusers over ComfyUI, no `torch.compile`, `refresh_worker` on OOM, fail-fast startup validation, never deploying `latest`, and the webhook-plus-polling resilience argument.
