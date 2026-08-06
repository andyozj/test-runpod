# Docs

Reading order: repo [`README.md`](../README.md) → [`DESIGN.md`](DESIGN.md) → the spec covering whatever you are looking at. Style, coverage and complexity rules live in [`STANDARDS.md`](../STANDARDS.md).

## Design and operations

| File | What it covers |
|---|---|
| [`DESIGN.md`](DESIGN.md) | Design decisions and trade-offs — why the system is shaped this way |
| [`RUNBOOK.md`](RUNBOOK.md) | Operational procedures: deploy, rollback, incident signatures. Written to be followed under time pressure by someone who did not write them |

## Specs

Dated 2026-08-05 onwards, governed by `STANDARDS.md`.

| Spec | What it covers |
|---|---|
| [`00-overview.md`](specs/00-overview.md) | Entry point for the spec set. Read first |
| [`01-worker.md`](specs/01-worker.md) | Tier 1, the graded deliverable: handler, worker lifecycle, cold vs warm |
| [`02-gateway-core.md`](specs/02-gateway-core.md) | Tier 2 domain layer and persistence; the job state machine. No HTTP, no RunPod vocabulary |
| [`03-facades.md`](specs/03-facades.md) | Transports over one `JobService`, each described by the cost it imposes on the caller. Async only |
| [`04-guardrails.md`](specs/04-guardrails.md) | Content safety at two checkpoints. `diffusers` FLUX pipelines ship no `safety_checker` |
| [`05-observability.md`](specs/05-observability.md) | Correlation-ID propagation and the serverless no-sidecar constraint |
| [`06-build-deploy.md`](specs/06-build-deploy.md) | Image layers, the two build-blockers, weight delivery |
| [`07-testing.md`](specs/07-testing.md) | Pyramid 70/20/10, 80% coverage, and the no-GPU-in-CI constraint that shapes the design |
| [`08-production-readiness.md`](specs/08-production-readiness.md) | An honest account of what this is not. Reconciled against the shipped code on 2026-08-06 |
| [`09-benchmarks.md`](specs/09-benchmarks.md) | Benchmark methodology. Every number in `BENCHMARKS.md` must survive being questioned |

Specs 00, 01, 02, 04, 05 and 06 carry Excalidraw diagram links in their headers.

## Working material

- [`notes/`](notes/) — historical working notes (platform alignment, how FLUX works, how RunPod works, status snapshots). Point-in-time records, not maintained.
- [`superpowers/`](superpowers/) — agent-workflow artifacts: the design spec and the audit-fix plan the work was driven from.
