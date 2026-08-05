# 07 — Testing

Governed by [`STANDARDS.md`](../../STANDARDS.md) §9. Pyramid 70/20/10, 80% coverage minimum, 100% on validation and the error-code contract.

## The constraint that shapes the design

**No unit or integration test may require a GPU, model weights, or an external network service.**

Local containers are permitted — testcontainers Postgres is expected. The prohibition is on *external* services: HuggingFace, the RunPod API, object storage. Those make the suite slow, flaky, dependent on someone else's uptime, and dependent on credentials CI should not hold.

The GPU half is architectural. It is why `pipeline.py` exposes a lazy accessor rather than a module-level global, and why `inference.generate()` takes a pipeline parameter instead of reaching for one. A handler that cannot be imported on a laptop cannot be tested, and the fix for that is design, not tooling.

It is also what makes Phases 1, 3, and 4 possible while credits are pending.

## Unit

| Area | Cases |
|---|---|
| Prompt validation | Blank, whitespace-only, at the T5 token limit, one token over. Over-limit **fails** rather than truncating — see [01](01-worker.md) |
| Dimension snapping | Parametrized boundaries: 255, 256, 1000, 1023, 1024, 1536, 1537 |
| Other parameters | Steps and guidance at, inside, and outside their ranges |
| Handler success | Fake `ImagePipeline`; asserts echoed seed, effective dimensions, `model_version` |
| Handler OOM | Fake raising `torch.cuda.OutOfMemoryError`; asserts `refresh_worker: True` and the `OOM` code |
| Handler failure | Fake raising a generic exception; asserts `INFERENCE_FAILED` and that internal detail is **not** in the response |
| Guardrails | Normalisation evasions, word-boundary false positives, chain severity, raise-blocks, timeout-blocks — see [04](04-guardrails.md) |
| `JobService` | In-memory fakes for both protocols; submit, get, reconcile, terminal-state transitions |
| Status mapping | Every RunPod status maps to exactly one domain status; unknown input raises |
| Auth | Valid, invalid, missing, malformed header; asserts constant-time comparison is used |
| Circuit breaker | Opens after N failures, fails fast while open, half-opens, closes on a successful probe |
| Retry | Retries 5xx and timeouts, does **not** retry 4xx, respects the attempt cap |

Seed determinism is asserted with the fake: the same seed produces the same recorded call. Actual pixel determinism is an E2E concern.

## Integration

| Area | Cases |
|---|---|
| Repository | Real Postgres via testcontainers. CRUD, `list_unresolved` ordering and limit |
| **Idempotency race** | **Genuinely concurrent inserts with the same key.** One wins, the other returns the existing job. A sequential test passes against the broken implementation and is therefore worthless |
| Migrations | Alembic up and down against an empty database |
| RunPod adapter | Mocked transport. Response parsing, status mapping, retry, breaker, timeouts |
| API | FastAPI `TestClient` end to end with a fake `RunPodClient`. Auth, submit, poll, 404, error envelope shape |
| Reconciler | Advances non-terminal jobs; never moves a terminal job backwards on an out-of-order response |

The out-of-order reconciler case is worth naming because it is the one that only appears under real concurrency and silently corrupts state when it does.

## E2E

Marked `@pytest.mark.gpu`, deselected by default, run by hand against a live endpoint. Blocked until Phase 2b.

| Case | Purpose |
|---|---|
| Prompt → image | The graded demonstration |
| Same seed twice | Reproducibility |
| Idempotency replay | Same key returns the same job, one generation billed |
| Oversized payload probe at 1536² | Measures the undocumented RunPod response ceiling |
| Cold start timing | Feeds `BENCHMARKS.md` |
| Blocked prompt | Guardrail path through the real stack |

## Doctests

Run for `core/` and `schemas.py` only:

```
uv run pytest --doctest-modules src/gateway/core src/gateway/schemas.py
```

Scoping matters — `--doctest-modules` across the whole package imports GPU-touching modules at collection and breaks the constraint above.

Per [`STANDARDS.md`](../../STANDARDS.md) §10, `Example` blocks are required only where genuinely runnable and forbidden where they cannot execute, so everything written as `>>>` is verified rather than decorative.

## Fakes, not mocks

Mock at boundaries, never internals. The boundaries are exactly the protocols in [02](02-gateway-core.md): `JobRepository`, `RunPodClient`, `ImagePipeline`, `PromptGuardrail`, `ImageGuardrail`, `Clock`.

Hand-written fakes over `unittest.mock` for these. A fake that records calls and returns realistic values catches contract drift; a `MagicMock` returns another `MagicMock` and asserts nothing about whether the interface was used correctly.

`Clock` is injected for the same reason — a service calling `datetime.now()` cannot have its timestamps asserted.

## What is not tested

- Actual image quality. No automated check; visual inspection during Phase 2b.
- Guardrail classification accuracy. The blocklist is tested for correct mechanics, not for whether the list is right.
- Load and concurrency behaviour. No load test — see [08](08-production-readiness.md).
