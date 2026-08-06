# Audit fixes — 2026-08-06 recon, all findings

Source: 5-lens recon (correctness, security, qa, quality, docs). Every finding below
was verified against file:line by a read-only auditor. Fix all of them.

## Global Constraints

- STANDARDS.md at repo root governs: ruff + mypy strict, 80% coverage floor per package,
  complexity limits (C901 max 12, PLR0915 max 50 statements), every `Any` justified inline,
  `.env.example` lists every variable, every package has README + pyproject + Dockerfile.
- Behavior changes require tests (TDD: failing test first where practical).
- `make check` must pass in worker/ and gateway/ after every task; `make lint-tools` for
  client/scripts/benchmarks after tasks touching them.
- Commit convention: conventional commits (`fix:`, `test:`, `ci:`, `docs:`, `chore:`).
  No AI attribution, no Claude-Session trailers, no Co-Authored-By.
- Cross-package contracts live in `contracts/` (JSON) — extend that pattern; never have
  worker import gateway or vice versa.
- Do not touch git history (the endpoint-id-in-history finding is accepted as-is).

## Task 1 — Gateway correctness: reconciler race cluster + service/adapter edge cases

Package: `gateway/`. Files: `src/gateway/core/service.py`, `src/gateway/adapters/memory.py`,
`src/gateway/adapters/runpod_client.py`, `src/gateway/core/protocols.py`, tests.

Invariants to enforce (each needs a test):

1. **No double-submit (P0).** A job whose upstream submit is still in flight must not be
   claimable by the reconciler. `create()` writes the row before awaiting `submit()`
   (service.py:114); a tick during that window adopts the row and submits a duplicate.
   Fix: `claim_unresolved` must implement real claim semantics — lease with expiry
   (protocol docstring at memory.py:193 already promises `FOR UPDATE SKIP LOCKED`
   semantics; deliver the in-memory equivalent) — AND rows with `runpod_job_id=None`
   younger than a submit-grace-period must be skipped.
2. **Idempotent replay never re-submits** (service.py:111). Replay hitting a QUEUED row
   with no `runpod_job_id` yet must return the existing job, not fall through to a second
   `runpod.submit()`.
3. **Cancel always reaches upstream** (service.py:156). Cancelling a job with
   `runpod_job_id=None` must record cancel intent; when `attach_runpod_id` later runs on a
   locally-cancelled job, issue `runpod.cancel(id)` then. No GPU job may keep running
   with no recorded id.
4. **Timeout cancels upstream** (service.py:191). `_expired` must call `runpod.cancel`
   when an id exists, before writing `TIMED_OUT`.
5. **Status mapping is honest** (service.py:224). Only mark `IN_PROGRESS` when upstream
   reports `IN_PROGRESS`, not merely because local status is QUEUED.
6. **COMPLETED requires an image** (runpod_client.py:171 + service.py:213). Upstream
   COMPLETED with missing/empty/non-dict `output` must resolve to FAILED with an error
   code from `contracts/error-codes.json` (pick the closest existing upstream-error code),
   never a terminal success with `image_base64=None`. Non-dict `output` must not raise
   `AttributeError` (runpod_client.py:171 vs :165).
7. **Terminal upstream statuses preserved** (runpod_client.py:303). `TIMED_OUT` and
   `CANCELLED` upstream map to our TIMED_OUT/CANCELLED, not blanket FAILED.
8. **Adapter error contract is total** (runpod_client.py:251, :149). Non-JSON 2xx body
   and missing `id` key raise `UpstreamUnavailableError`, not `JSONDecodeError`/`KeyError`.
9. **Half-open breaker single probe** (runpod_client.py:92). After cooldown, exactly one
   probe is admitted; others fast-fail until the probe resolves (docstring already
   promises this).
10. **Health staleness bound** (service.py:284). `_check_queue_pressure` must treat a
    health snapshot older than a max-age (new setting, default ~30s) as unknown → fail
    open. A dead reconciler must not shed traffic forever.
11. **Replay detection consults the repository** (api/app.py:168). `Idempotency-Replayed`
    header must be set from whether the repo returned an existing row, not from
    `correlation_id` equality.

## Task 2 — Gateway security: fail-closed auth, log hygiene, per-key cap

Package: `gateway/` + `compose.yaml`. Files: `src/gateway/settings.py`, `src/gateway/api/app.py`,
`src/gateway/core/service.py` (cap), tests.

1. **Fail closed** (settings.py:38). Remove the `demo:local-development-key` fallback.
   `GATEWAY_API_KEYS` unset/empty → startup fails with a clear error. `compose.yaml:21`
   keeps a dev key via explicit env default in the compose file only (opt-in by running
   compose locally) — the application itself never invents a credential.
2. **No secret material in logs** (settings.py:62). Malformed key-pair warning must not
   include any part of the raw value — log the pair index and reason only. Test the
   malformed-pair path (currently untested).
3. **Per-key active-job cap** (app.py:107 / service). New setting
   `MAX_ACTIVE_JOBS_PER_KEY` (default 10). `create()` rejects with 429 + Retry-After when
   the caller's non-terminal job count is at the cap. Repo method to count active jobs by
   `api_key_id`. Tests: at-cap rejection, under-cap acceptance, terminal jobs don't count.
4. `.env.example` + `gateway/README.md` updated for the new/changed variables (Task 6 does
   the full docs pass; here just keep them truthful for what this task changes).

## Task 3 — Gateway QA gaps: untested caller-facing paths

Package: `gateway/`. Tests only (plus minimal test hooks if strictly needed).

1. 429 `QUEUE_SATURATED` HTTP mapping incl. `Retry-After` header (app.py:148-158).
2. 503 `UPSTREAM_UNAVAILABLE` HTTP mapping incl. body + `Retry-After: 5` (app.py:137-147).
3. Cross-tenant access: caller B GET/cancel of caller A's job → 404, response
   indistinguishable from unknown id (app.py:194, :217).
4. `/health/detailed`: assert body; drive the reconciler-stall branch via
   `Deps.reconciler_age` fixture → `"degraded"`, `stalled: true`, `last_tick_s` (app.py:320-340).
5. Adapter wired-path tests: 3 consecutive transport failures through `_request` →
   `UpstreamUnavailableError` + breaker opens; open breaker → fast-fail without transport
   call (runpod_client.py:211, :254). Real-transport tests for `cancel` URL path
   (`cancel/{id}`) and in-progress partial-output parse (runpod_client.py:198, :180).
6. `InMemoryJobRepository`: oldest-first ordering + `limit` of `claim_unresolved`
   (post-Task-1 semantics), idempotency-key eviction (memory.py:95-101).
7. Reconciler unit tests (workers/reconciler.py): tick cadence, idle backoff, loop
   survives an exception, clean cancellation on shutdown, `seconds_since_last_run`,
   `running` flag. (Spec docs/specs/07-testing.md:85 lists these.)

## Task 4 — Worker fixes + cross-package contract conformance

Packages: `worker/`, `gateway/`, `contracts/`.

1. **Raising guardrail blocks, not crashes** (worker/src/worker/handler.py:78-83).
   Catch non-`WorkerError` exceptions from guardrail checks and convert to a blocked/
   error result per error-codes contract. Use the existing `RecordingGuardrail.raises`
   fake (worker/tests/conftest.py:60) in a test.
2. **Shared normalisation contract.** Extract the duplicated `_INVISIBLE`, `_SEPARATORS`,
   `_CONFUSABLES` tables (worker/guardrails.py:30, gateway/adapters/guardrails.py:24)
   into `contracts/normalisation.json`; both packages load it (same pattern as
   `blocklist.json`). Conformance tests in BOTH packages assert the loaded tables match
   the contract file. Verify worker Dockerfile copies contracts/ (blocklist already
   loads from there — follow the same mechanism).
3. **Real blocklist conformance** (gateway/tests/unit/test_blocklist_conformance.py,
   worker/tests/unit/test_guardrails.py). Shared corpus: add a small
   `contracts/guardrail-corpus.json` of (input, expected-verdict) cases — including
   confusable/invisible-char evasions — and both suites run the full corpus against
   their own implementation. Dropping a confusable from either tier must fail that
   tier's suite.
4. **Schema conformance tests.** Both packages: test that their Pydantic request models
   (worker/schemas.py:9-11,37; gateway/api/schemas.py:13-15,18) agree with
   `contracts/generation-request.schema.json` bounds (min/max dimension, prompt length).
   Same for the `ErrorCode` enums vs `contracts/error-codes.json` loader behavior
   (worker/errors.py:12, gateway/core/models.py:58).
5. **JPEG branch test** (worker/inference.py:39-42) via `output_format="jpeg"`.
6. **Pipeline module test** (worker/pipeline.py:44): `get_pipeline` memoises (second call
   returns same object, loader called once).
7. **Seed assertion strengthened** (worker/tests/unit/test_handler.py:70-76): assert the
   generated seed reached the pipeline call and lies in `[0, MAX_SEED)`.
8. **`huggingface-hub` to runtime deps** (worker/pyproject.toml:33) — fetch_weights.py
   imports it and runs in a `--no-dev` image (Dockerfile:55,71).
9. Unjustified `Any`s get inline justification or a real type: worker/handler.py:182,
   worker/inference.py:121 (gateway ones handled where touched: app.py:227,277,
   runpod_client.py:281,311; protocols.py:223 `PromptGuardrail.check` gets a typed
   verdict Protocol — kill the `getattr` workaround at service.py:106).

## Task 5 — CI/CD, tooling, container hardening

Files: `.github/workflows/deploy.yml`, `.github/workflows/ci.yml`, `gateway/Dockerfile`,
`ruff.toml`, `.pre-commit-config.yaml`, `Makefile`, `.gitignore`, `benchmarks/harness.py`,
`scripts/apply_endpoint.py`, `deploy/endpoints/baked.yaml`, `compose.yaml`.

1. **deploy.yml script injection** (:97, :99): move `${{ inputs.* }}` out of `run:` blocks
   into `env:` indirection.
2. **Tag deploys gated** (:63, :101): `checks` job runs on `workflow_dispatch` too
   (remove `if: ${{ !inputs.tag }}`); deploy job verifies the image tag exists in GHCR
   before applying (`docker manifest inspect` or GHCR API).
3. **Post-deploy smoke gate**: new job after apply — runs
   `uv run pytest -m gpu tests/e2e` in worker/ against the deployed endpoint using the
   workflow's secrets; deploy marked failed if it fails. The e2e suite must FAIL (not
   skip) when env vars are absent in CI context (worker/tests/e2e/test_endpoint.py:37 —
   make skip conditional on not-CI, e.g. fail if `CI` is set and vars missing).
4. **Pin third-party actions to commit SHAs** in deploy.yml (:64 docker/login-action, :91
   astral-sh/setup-uv, and all others in both workflows).
5. **Gateway container non-root** (gateway/Dockerfile:25): add uid-1000 user like
   worker/Dockerfile:91-92.
6. **ruff actually covers the tools** (ruff.toml:6): include `benchmarks/**`; add
   PLR0915/PLR2004 and pylint limits per STANDARDS §6. Then fix everything that lights
   up in `benchmarks/harness.py` — including refactoring `render()` (harness.py:580,
   193 lines / complexity 30) and `_product_sections` (:502, complexity 17) below the
   C901=12 / 50-statement limits, and format-check.
7. **Harness zero-division** (harness.py:618, :648): guard `cost_28`/`main_p50` == 0 →
   emit "n/a" instead of crashing; `render` must not run for `--only` modes lacking data.
8. **apply_endpoint.py tests + mypy**: add `scripts/tests/test_apply_endpoint.py` covering
   the `latest`-tag refusal (:282) and yaml→payload mapping (:208, pure functions);
   add mypy for `scripts/` (and `client/`, `benchmarks/` if cheap) to CI tools job.
9. **Pre-commit parity** (.pre-commit-config.yaml:18-29): ruff/ruff-format cover client/
   scripts/benchmarks too; add gateway mypy hook; add `lint-imports` hook.
10. **Makefile** (:34, :44-91): `make check` includes doctest (parity with CI); all
    targets get `##` help markers; doctest scope per STANDARDS §9 includes
    core/service.py (fix STANDARDS or Makefile, whichever is wrong — prefer running
    doctests over all of gateway core).
11. **.gitignore**: add `.claude/settings.local.json`, `.coverage`, cache dirs
    (`.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`, `.import_linter_cache/`).
12. **baked.yaml registry placeholder** (deploy/endpoints/baked.yaml:15):
    `ghcr.io/OWNER/flux-worker` → `ghcr.io/andyozj/flux-worker`.
13. **HTTP status constants** (gateway/api/app.py:30-32 vs raw literals): use the
    constants everywhere or drop them — pick one, apply consistently. (Coordinate: app.py
    also touched in Tasks 1-3; this is a mechanical sweep at the end.)
14. **Function-local imports** (app.py:127, :350, models.py:165): move to module level.
15. **Duplicated generation defaults** (service.py:29-31 vs settings.py:42-44): single
    source — service constants become the Settings defaults' source or vice versa.
16. **compose.yaml passthrough vars** stay in sync with `.env.example` (Task 6 finishes).

## Task 6 — Docs truth pass

Files: `README.md`, `docs/specs/*.md`, `docs/RUNBOOK.md`, `docs/notes/where-we-are.md`,
`.env.example`, `gateway/README.md`, new `worker/README.md`, `STANDARDS.md` (§9 only if
Task 5 chose Makefile), `gateway/api/schemas.py` (examples).

1. README.md:38 — true-cold p50 is 89.9s (max 518.1s) per BENCHMARKS.md:22; fix the 304s
   claim. README.md:250 — test count 73 unit + 7 e2e (re-run collection to confirm exact
   number AFTER tasks 1-5 added tests; update all counts).
2. Specs: purge `contracts/model-revision.txt` (06-build-deploy.md:33, 07-testing.md:32,:54 —
   describe actual discovery behavior of worker/weights.py); Pod-build claim
   (06:114,:194 → local/CI build reality); postgres/migrations compose claim (06:134 →
   gateway-only, in-memory); `GATEWAY_API_KEY` → `GATEWAY_API_KEYS` (06:133);
   no-cancel-route claim (02-gateway-core.md:175 → cancel exists);
   CUDA base image claim (06:75 → ubuntu:22.04); `WEIGHTS_PATH` guidance
   (09-benchmarks.md:119 → must be unset, MODEL_CACHE_ROOT used, per RUNBOOK:71);
   `AVG_JOB_SECONDS` → `AVG_JOB_S` (08:47, 09:55, 02:307); version-injection claim
   (05-observability.md:104 → either implement VERSION build-arg injection in
   gateway/Dockerfile + deploy.yml, or fix the doc — implement it, it's small: ARG →
   env → settings.version, and document); baked-image published claim
   (00-overview.md:61,:137 → align with README:176 "buildable on demand").
3. `.env.example`: every variable both settings modules read + compose passthroughs
   (RECONCILE_INTERVAL_S, RECONCILE_IDLE_INTERVAL_S, RECONCILE_BATCH, JOB_DEADLINE_S,
   MAX_QUEUE_WAIT_S, AVG_JOB_S, VERSION, MODEL_CACHE_ROOT, MODEL_ID, MODEL_REVISION,
   GHCR_USER, GHCR_TOKEN, plus new Task 1/2 settings), one-line description each.
4. RUNBOOK: teardown renumbering (:167); add the e2e reproduce command
   (`cd worker && uv run pytest -m gpu tests/e2e`) to the Verify section; README Develop
   section gets it too; add GHCR_USER/GHCR_TOKEN to the rotation checklist (:168).
5. `docs/notes/where-we-are.md`:44,:47 — correct the reversed statements (volume fallback
   dropped; revision pin removed) or clearly mark the note as historical.
6. `worker/README.md` — new, mirroring gateway/README.md structure (STANDARDS §3).
7. Missing schema examples (gateway/api/schemas.py:61,:69,:84): add `json_schema_extra`
   examples to JobCreated, ErrorBody, JobView per STANDARDS §10.
8. Every number/command claimed in docs must be re-verified against the post-fix tree.
