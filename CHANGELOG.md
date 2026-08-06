# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow the `v*` git tags, which also drive the image tag (`<version>-<sha>`).

## [Unreleased]

43 commits since `v0.1.0`: an audit-fix pass over correctness, security, CI and docs.

### Added

- Continuous deployment: `deploy.yml` builds, pushes, applies the endpoint config and runs a post-deploy smoke test from one button; a `v*` tag push publishes the image without deploying. Rollback is a re-run with the previous tag.
- Tag-driven versioning across the Makefile, image tags and `make print-tag`.
- CI gates both packages, the `import-linter` layering contract and the CLI tools (`client/`, `scripts/`, `benchmarks/`).
- Benchmarks: queue-depth, FlashBoot and cross-GPU sections; the harness now restores endpoint state. Report grew from 129 to 156 records, including an A100 cross-tier run.
- Unit tests for `scripts/apply_endpoint.py` pure functions.
- OpenAPI examples for `JobCreated`, `ErrorBody` and `JobView`.
- Shared guardrail contracts: `contracts/normalisation.json` and `contracts/guardrail-corpus.json`, loaded by both the worker and the gateway.
- Per-key active-job cap (`MAX_ACTIVE_JOBS_PER_KEY`, default 10).
- `make help` markers on every target; `make check` now matches the CI doctest gate exactly.

### Changed

- Worker reports the model revision it loaded instead of pinning one.
- Typed guardrail verdict, one status convention, no function-local imports in the gateway.
- `VERSION` injected at gateway build time; every setting passed through `compose.yaml`.
- README restructured into three tiers (deliverable, benchmarks spike, gateway spike); `.env.example`, specs 03/09 and the working notes reconciled against the tree.
- Gateway tests: 57 → 187 test functions (240 collected).

### Fixed

- Reconciler double-submitted a job mid-submit. Closed with a lease plus a submit grace derived from the client's worst-case retry envelope.
- Stranded circuit-breaker probes are freed.
- The RunPod adapter's error contract is now total.
- Idempotency replay is resolved before load-shedding; a shed request releases its idempotency key and gets a jittered `Retry-After`.
- Worker guardrail crashes report `INFERENCE_FAILED` rather than a block; vacuous allow-cases in the guardrail corpus fixed.
- Demo client surfaces worker error envelopes and bounds its polling loop.
- `BENCHMARKS.md` is no longer rendered from a partial `--only` run.
- `deploy/endpoints/baked.yaml` registry-owner placeholder.

### Security

- Gateway fails closed on a missing or blank `GATEWAY_API_KEYS`; no built-in credential remains. Raw key material is no longer logged.
- Guardrails fail closed on a crash at both tiers; normalisation tables load from the shared contract instead of a per-tier copy.
- Per-key non-terminal job cap bounds how much billable queue one key can hold.
- CI hardening: third-party actions pinned to commit SHAs, `deploy.yml` injection risk removed (inputs passed via `env:`), checks gated before deploy, tag verified, smoke test added.
- Gateway container runs as a non-root user.
- Endpoint id redacted from the committed status note.

## [0.1.0] - 2026-08-06

First live deployment. Endpoint created from image `0.1.0-44c9643-slim`.

### Added

- **Worker**: `black-forest-labs/FLUX.1-dev` handler with Pydantic-validated input, dimensions snapped to ×16, the effective seed always echoed back, progress updates at 10% strides, and a stable `code`/`message`/`suggestion` error envelope.
- **Image**: one `Dockerfile`, two variants via `BAKE_WEIGHTS` — slim (~2.9GB, no weights, deployed) and baked (~45GB, built on demand). Non-root user; installed from the committed lockfile.
- **Endpoint**: config-as-code in `deploy/endpoints/` applied through the RunPod REST API by `scripts/apply_endpoint.py`; workers bounced on every update.
- **Benchmarks**: resumable harness plus the first measured run — 129 records, one card, zero failures.
- **Gateway spike**: domain core, adapters, async API and reconciler, behind an `import-linter` layering contract.
- Demo client on RunPod's Python SDK; `compose.yaml` for a local gateway.
- GPU-marked e2e suite against the live endpoint, written before deploy.
- `STANDARDS.md`, specs `00`-`09`, `docs/RUNBOOK.md`, six architecture diagrams, committed samples with seeds.

### Changed

- Weight delivery moved from a network volume to RunPod cached models; the volume was tried, measured against the alternative, and removed entirely rather than kept as an unpopulated fallback.
- Redundant API surface deleted in favour of RunPod's own operations.

### Fixed

- Error envelopes are JSON-encoded into the platform's `error` string; a dict returned there is silently dropped, leaving the caller a completed job with no output.
- Endpoint `PATCH` bodies exclude create-only keys; L40S only.
- Contract paths resolve by search rather than a fixed parent depth.

[Unreleased]: https://github.com/andyozj/test-runpod/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/andyozj/test-runpod/releases/tag/v0.1.0
