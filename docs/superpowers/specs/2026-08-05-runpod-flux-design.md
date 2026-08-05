# FLUX.1-dev on RunPod Serverless — Design (superseded)

**Superseded 2026-08-05** by the spec set in [`docs/specs/`](../../specs/00-overview.md).

This document grew past the point where a single file was useful. It has been split by concern, and the content extended with guardrails, authentication, upstream resilience, health endpoints, and a production-readiness gap analysis.

Start at [`docs/specs/00-overview.md`](../../specs/00-overview.md).

| Spec | Covers |
|---|---|
| [00 — Overview](../../specs/00-overview.md) | Context, scope, phase plan, risks, corrections |
| [01 — Worker](../../specs/01-worker.md) | Handler, pipeline, inference, RunPod contract |
| [02 — Gateway core](../../specs/02-gateway-core.md) | Domain model, `JobService`, protocols, auth, resilience |
| [03 — Facades](../../specs/03-facades.md) | Transports and what each costs the caller |
| [04 — Guardrails](../../specs/04-guardrails.md) | Prompt and image safety hooks |
| [05 — Observability](../../specs/05-observability.md) | Logging, correlation, health |
| [06 — Build & deploy](../../specs/06-build-deploy.md) | Image, weights, CI, deploy, rollback |
| [07 — Testing](../../specs/07-testing.md) | Strategy across both tiers |
| [08 — Production readiness](../../specs/08-production-readiness.md) | What is deliberately not built |

Retained here only as a record of where the design started. Do not edit.
