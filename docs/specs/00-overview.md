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

> **Diagram:** system context — *pending Excalidraw*

## Brief

Deploy a serverless endpoint on RunPod running an ML model, accepting a text prompt and returning a generated image. Deliverables: `handler.py`, a Docker image containing the handler **and the model**, a deployed endpoint, a demonstration, and documentation.

Graded on: following the setup instructions, handler/model integration, successful deployment, documentation clarity.

## Scope

| Decision | Value |
|---|---|
| Model | `black-forest-labs/FLUX.1-dev`, bf16, unquantized |
| Inference | `diffusers.FluxPipeline`. No `torch.compile`, no ComfyUI |
| Weights | Baked into the image, per the explicit instruction |
| Build host | RunPod GPU Pod |
| Registry | GHCR |
| Tiers | Serverless worker (graded) + FastAPI gateway (beyond the brief) |
| Primary facade | **Async** — submit, poll |
| Also built | Sync wrapper (demo-grade), API-key auth, retry + circuit breaker, health endpoints, prompt guardrails |
| Documented only | Webhook-out, SSE, WebSocket, MCP, rate limiting, metrics export |
| Out of scope | Fine-tuning, LoRA, batch inference, multi-region |

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
| 2b | **Build on Pod, push, deploy, smoke test, benchmark** | **Image generated; `BENCHMARKS.md` populated** | **Credits** |
| 3 | Gateway core, adapters, migrations, auth, tests | Coverage gate | No |
| 4 | Async + sync facades, reconciler, health, compose | E2E against a fake `RunPodClient` | No |
| 5 | Client demo, README, docs | Complete except measured numbers | Partly |

Splitting 2a from 2b is what makes the wait productive: it verifies both build-blockers in [06](06-build-deploy.md) with no GPU and no download.

Until 2b runs, `BENCHMARKS.md` does not exist and no figure anywhere is stated as measured.

## Risks

| Risk | Mitigation |
|---|---|
| **RunPod credits pending — materialised, not hypothetical** | Phases 0, 1, 2a, 3, 4 proceed locally; 2b reduced to execution. Escalate if no reply within 24h |
| HF licence gate blocks even the offline weight-filter check | Accept immediately; instant and independent of the credits reply |
| Gateway consumes time the graded deliverable needs | 2a completes before Phase 3 starts |
| ~45GB image push is slow and failure-prone | Build and push from the Pod; GHCR avoids Docker Hub pull limits |
| L40S unavailable in region | GPU fallback list; benchmark A100 80GB regardless |
| `diffusers` API drift | Pin exact versions; `uv.lock` committed |

## Corrections to earlier design notes

1. **Weights baked into the image, not a network volume.** The brief requires it. The volume approach is the documented production evolution.
2. **Cost figures were wrong by ~12×.** L40S serverless is $1.75/hr, not $0.54/hr — $0.99/hr is the *Pod* rate. 1024²/28 steps is ~20-25s, not 4-6s. Every figure is now measured or labelled an estimate.
3. **Module-level pipeline init replaced by a lazy accessor.** The original made `handler.py` unimportable without a GPU, therefore untestable.
4. **CI does not build the image.** Standard GitHub runners have ~14GB free disk.
5. **Gated-repo handling and the duplicate-weights trap added.** Both were absent; both block the build.
6. **Async promoted to the default facade**, which also resolves the payload-ceiling unknown.

Retained as correct: diffusers over ComfyUI, no `torch.compile`, `refresh_worker` on OOM, fail-fast startup validation, never deploying `latest`, and the webhook-plus-polling resilience argument.
