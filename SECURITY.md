# Security

What this repository actually does, with file pointers. Gaps are listed at the bottom rather than omitted.

## Authentication

Fail-closed by construction. [`gateway/src/gateway/settings.py`](gateway/src/gateway/settings.py):

- `GATEWAY_API_KEYS` is a required setting with no default. A blank or whitespace-only value raises in a `field_validator`, so the process dies at startup instead of admitting an undocumented caller on the first request.
- Format is comma-separated `key_id:secret` pairs. A pair without `:` is skipped and logged as `malformed_api_key_pair` — a silent drop would turn a config typo into a lockout with no evidence.
- Secrets are compared as SHA-256 digests via `hmac.compare_digest`. Constant-time comparison closes the prefix leak in `==`, which short-circuits on the first differing byte.
- No key material is logged. A failed auth emits `auth_failed` and nothing else.

Missing, malformed, or unknown `Authorization: Bearer` → `401 UNAUTHENTICATED` ([`gateway/src/gateway/api/app.py`](gateway/src/gateway/api/app.py)).

## Tenant isolation

Every job carries the `api_key_id` that created it. Reads and cancels compare it against the caller and return `404 JOB_NOT_FOUND` on mismatch (`api/app.py`) — a job belonging to another key is indistinguishable from a job that does not exist, so the id space leaks nothing.

## Per-key resource cap

`MAX_ACTIVE_JOBS_PER_KEY` (default 10, `core/service.py::_check_active_job_cap`) bounds a key's concurrent non-terminal jobs against `repository.count_active(api_key_id)`. It is a share-of-queue control on billable work, not a request-rate limit.

## Guardrails

Two checkpoints, one shared contract. Terms come from [`contracts/blocklist.json`](contracts/blocklist.json) (3 categories: `csam`, `graphic_violence`, `self_harm`) and the evasion-resistant normalisation tables from [`contracts/normalisation.json`](contracts/normalisation.json) — NFKC, combining-mark stripping, invisibles, separators, confusables. Both the gateway adapter and the worker load the same two files, so the tiers cannot drift.

- **Gateway:** the prompt check runs before the insert (`core/service.py`), so a blocked prompt never becomes a job.
- **Worker:** `_guard_prompt` runs before any GPU time; `_guard_image` runs on the decoded bytes before the image is returned or uploaded (`worker/src/worker/handler.py`).
- **A guardrail that raises still stops the request** — fail closed — but is reported as `INFERENCE_FAILED`, not `PROMPT_BLOCKED`/`IMAGE_BLOCKED`: a crash is an infra fault, a block a policy verdict (rationale in [`docs/DESIGN.md`](docs/DESIGN.md) §11).
- The image hook is bound to `NoopImageGuardrail`: the extension point is exercised with real bytes at the right moment, but **no image classifier is running**. It blocks nothing today.

`diffusers` FLUX pipelines ship no `safety_checker`. Whatever is not listed above does not exist.

## Secrets

- `HF_TOKEN` reaches the build through a BuildKit secret mount (`RUN --mount=type=secret,id=hf_token`, [`worker/Dockerfile`](worker/Dockerfile)) — never `ARG`, never `ENV`, never a `COPY`'d file. An `ARG`-passed token is recoverable from image history.
- Runtime secrets arrive from the RunPod endpoint environment, never the image.
- `.env` is git-ignored; `.env.example` is committed with descriptions and no values.
- `detect-private-key` runs as a pre-commit hook.
- `HF_TOKEN` and `RUNPOD_API_KEY` must never appear in output (`STANDARDS.md` §8).

## Build and CI hardening

- Third-party actions are pinned to commit SHAs in both [`ci.yml`](.github/workflows/ci.yml) and [`deploy.yml`](.github/workflows/deploy.yml).
- `workflow_dispatch` inputs are passed through `env:` and referenced as shell variables, never interpolated into a `run:` body.
- `deploy.yml` calls `ci.yml` as a gate before it builds, and a `v*` tag push publishes without deploying.
- Both images run as a non-root user (`USER worker`, `USER gateway`).
- `bandit` rules (ruff `S`) run on every commit; `uv.lock` is committed.

## Known and accepted

- **The RunPod endpoint id is recoverable from git history.** It was redacted from the working docs in `17ebd0c`; earlier commits retain it. Accepted: the id is not a credential and is useless without `RUNPOD_API_KEY`.
- **No request-rate limit and no spend cap.** The per-key active-job cap bounds concurrency only; a key staying under it can submit indefinitely. There is no per-IP control. Top-ranked open gap; see [`docs/DESIGN.md`](docs/DESIGN.md#known-limits).
- **The gateway job store is in-memory.** Postgres is specified, not implemented; nothing survives a restart.

## Reporting

A personal portfolio repository, not a service with users. Open a GitHub issue on this repo. There is no on-call rotation and no response-time commitment.
