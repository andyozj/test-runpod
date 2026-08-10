# Frontend (spike)

The cockpit for the gateway: three views — **generate**, **ledger**, **operate**. **A spike, beyond the brief**, like the gateway: the graded deliverable is the worker, and the endpoint is callable without any of this.

React 19 + Vite 8 + TypeScript + Tailwind 4. Eight runtime dependencies: `react`, `react-dom`, `tailwindcss`, `@tailwindcss/vite`, `motion`, `thumbhash`, and two `@fontsource-variable` font packages. No router, no state library, no chart library, no component kit.

## Identity

"The instrument" — a council verdict, derived rather than picked (`docs/superpowers/decisions/DECISION_LOG.md`, 2026-08-09). A mission-control surface for a GPU fleet of one: every visual element renders telemetry the backend already emits, and lab-bench aesthetics (single dark theme, monospace numerics, dense data rows, one amber accent `oklch(0.78 0.14 70)`) appear as consequence, not decoration. WebGL was cut entirely. The design rule that follows: *if an element doesn't render backend data, it justifies itself or dies*.

## The three views

Routing is 83 lines of `history.pushState` over `useSyncExternalStore` (`src/lib/router.ts`): `/` and `/generate`, `/gallery`, `/operate`, `/jobs/:id` (the ledger with its detail dialog open), `?status=FAILED` (the ledger pre-filtered). Unknown paths `replaceState` to `/generate`, so back never re-404s. Tabs are real `<a href>` elements.

### Generate

- **Single** — one job. Seed locked, or omitted so the server picks it (the only path where the seed is not client-chosen).
- **Batch** — 1, 2 or 4 parallel cells, same prompt and parameters, only the seed varying: `seed, seed+1, …` when locked, otherwise one `crypto.getRandomValues` uint32 per cell.
- **Sweep** — exactly 4 cells, one parameter varying, seed locked across all four (entering sweep with the dice active locks a fresh random seed, visibly). Grids: steps `4 / 12 / 20 / 28`, guidance `1 / 3.5 / 7 / 10`, size `512 / 768 / 1024 / 1280` px². Each cell is stamped with its varied value and its measured wall time; the footer prints the whole spread (`4→3.1s · 12→9.1s · …`).

All three run through one code path — the single job is the N=1 case. POSTs are issued sequentially so cell order is deterministic; polling runs in parallel per cell.

**Polling.** `POST /v1/jobs` with an `Idempotency-Key`, then `GET /v1/jobs/{id}` every 1000 ms for the first 60 s and every 2000 ms after. Every request carries a 30 s `AbortSignal.timeout`. A transient (non-HTTP) poll failure retries in 2000 ms and does not surface. The idempotency key is reused only while no job id is known — a 429 shed or a dropped response then replays instead of billing a second generation.

**Streaming denoise previews.** `progress.preview_b64` (a ≤15 kB JPEG the worker emits mid-run) is rendered as a full-cell frame; the last two frames stay mounted and the newer one fades in over 0.3 s. On terminal the wire contract drops the preview, and the last frame survives 600 ms longer as an underlay so the final image crossfades out of it rather than popping in.

**Stage timeline.** Three stages — `queued`, `inference`, `finalize` — derived client-side from what the polls actually observed, each stamped with elapsed ms. A queue longer than 30 s is annotated *likely cold start — platform staging can take minutes*, with the measured cold numbers in the tooltip. Batch and sweep collapse to one aggregate timeline (cells per stage plus the slowest cell's elapsed); it folds `finalize` into the `inference` row.

**Failures** render as their structured envelope: code chip, message, suggestion-as-action, copyable correlation id. `Retry-After` becomes a live countdown that disables the retry button (`retry in Ns`). `PROMPT_BLOCKED` / `IMAGE_BLOCKED` / `INVALID_PROMPT` swap the action to *change prompt* — a policy verdict is not retryable. Cancel works before the job id exists (it targets the pending submit) and after.

**Params rail.** Model (static), size (256–1536 in 16px steps, default 1024), steps (1–50, default 28), guidance (0–20 step 0.1, default 3.5), seed (0–2³¹−1) with dice/lock, mode (batch/sweep), batch size, sweep parameter, format (png/jpeg). The parameter a sweep owns is disabled and displays its swept range instead of a stale constant. Below the controls: live job count, gateway health dot, model revision, and the session wall-time sparkline. Below `lg` the rail becomes an Escape-dismissable drawer.

### Ledger

`GET /v1/jobs?limit=100`, one fetch per activation — the reproducibility ledger. Cards are grouped by UTC day and carry seed, size, model revision, inference seconds and status as chips; clicking a chip filters the list by that value (one filter at a time; `?status=` is adopted from the URL and cleared back out of it). Thumbnails are ThumbHash placeholders crossfading into the loaded image; an image evicted by the gateway's byte-capped store renders `image evicted · 410` rather than a broken tile.

The two split verbs no image tool separates:

- **rerun seed** — resubmits the job's *own* recorded config (prompt, size, steps, guidance, format) with `seed` locked. Reproduces the image bit for bit. Disabled when the job has no seed.
- **reroll · new seed** — the same recorded config with the seed dropped and the dice re-armed.

Both prefill the Generate view from the job record, never from the rail's current state.

**Detail overlay** (`/jobs/:id`, a real history entry, deep-linkable): the image at its true aspect ratio, and every field — prompt, status, error code and message, seed, size, steps, guidance, format, model revision, inference seconds, wall time, timestamps, correlation id, job id. `copy as json` yields the full `JobView`; `copy as curl` yields a `POST /v1/jobs` command carrying the full recorded request, so the generation reproduces from a terminal. `←`/`→` step through the filtered list (one history entry, `replaceState`).

**Lightbox**: fit-to-viewport up to 1:1, click or `z` toggles 100%, `+`/`-` and the wheel zoom, drag or arrows pan, `esc` closes. The HUD states zoom percent, true pixel dimensions and seed.

### Operate

`GET /v1/metrics` every 10 s, and only while the tab is visible and the view is on screen — a backgrounded tab must not poll a GPU-adjacent endpoint for nothing. The header states how old the snapshot is.

| Panel | Contents |
|---|---|
| endpoint | in queue, in progress, workers running, workers idle (shared upstream topology), plus this key's active-now and last-hour counts. Reading age and reconciler liveness underneath |
| latency | p50/p95/max of inference and wall time, as a table and as bars on a shared scale, with a marker at the committed bench p50 of 21.8 s |
| throughput | completions per UTC hour, 24 dense buckets; a zero hour keeps its slot as a stub, never a gap |
| jobs | all-time counts per status; a non-zero count links to `/gallery?status=…` |
| cost | estimated per job and per window — the only estimated surface, labelled `est.` and *estimated, not billing data* |

A queue reading older than 30 s carries a `STALE` chip and says the readouts are the last cached poll; a missing reading says the readouts are unknown, not zero; a reconciler past 60 s without a tick turns warn. A poll failure keeps the last good snapshot on screen behind a `POLL FAILING · LAST GOOD READING` chip. With no completed jobs in the window the panels read their honest zeros and say so.

## The teaching contract

Every duration on screen is measured — client-observed (`performance.now()` from submit to terminal) or server-reported (`inference_seconds`, `progress`). Estimates are labelled `~` and sourced from [`BENCHMARKS.md`](../BENCHMARKS.md). There are exactly two of them: the Generate button's `~22s warm`, and `~34GB` in the cold-start tooltip.

The sourced constants live in one file, `src/lib/bench.ts`, each carrying the table it came from:

| Constant | Value | Source |
|---|---|---|
| `BENCH_WARM_EXEC_P50_S` | 21.8 s | steps sweep, 28 steps at 1024², N=10, exec p50 |
| `BENCH_WARM_EXEC_MIN_S` / `MAX_S` | 3.8 s / 38.2 s | the same sweep's fastest and slowest cells (4 and 50 steps) — the sparkline's density band |
| `BENCH_RECORD_COUNT` | 156 | committed benchmark records; the session's first job is "data point 157" |
| `COLD_TOOLTIP` | cold p50 90 s, resume 16 s, warm 0.1 s | cold-start table, quoted wherever a cold queue is flagged |

The sparkline plots the session's own wall times against that band, hollow-marking any run whose queue exceeded 3 s as a likely cold start. Cost is the only panel that says *estimated*, and it states its basis: measured execution seconds × the GPU rate, over the metrics window.

## Run it

```bash
cd frontend && npm ci
```

**Against a local gateway** (compose stack from the repo root, [`gateway/README.md`](../gateway/README.md#run-it)):

```bash
npm run dev          # http://localhost:5173
```

Vite proxies `/v1` and `/health` to `http://localhost:8000`, so there is no CORS and no base-URL configuration. Paste `demo:local-development-key` into the key popover — or press *use demo key* on the pre-key state. The key is kept in `localStorage`, sent as `Authorization: Bearer`, and cleared on any 401.

**Against the in-browser mock** — no gateway, no GPU, no credentials:

```bash
npm run dev:mock     # VITE_MOCK=1; MSW service worker, footer reads "mode mock"
```

The mock (`src/mocks/handlers.ts`) implements the whole wire contract: idempotent replay, correlation ids on every response, staged progress with real preview JPEGs, seed-keyed image variants (same seed → same bytes), a 12-job static ledger, and `/v1/metrics` computed from the same jobs the ledger answers with, so the dashboard cannot contradict the ledger.

Scenarios are selected by a token **inside the prompt**, `demo:`-prefixed so ordinary prompts containing "cold" or "blocked" behave normally. A prompt may carry several (`demo:shed-one demo:mixed`).

| Token | What it does |
|---|---|
| `demo:cold` | 45 s queue before inference — the cold-start annotation and the hollow sparkline point |
| `demo:hold` | parks at IN_PROGRESS 60% forever; the deterministic mid-denoise state the visual baselines capture |
| `demo:blocked` | queues, then `BLOCKED` / `PROMPT_BLOCKED` with its suggestion |
| `demo:oom` | runs to 40%, then `FAILED` / `OOM` |
| `demo:mixed` | per-cell divergence across a batch: cell 0 completes, cell 1 fails OOM mid-preview, the rest time out |
| `demo:shed` | 429 `QUEUE_SATURATED`, `Retry-After: 22`, once per idempotency key; the retry succeeds |
| `demo:shed-one` | 429 with `Retry-After: 8` for one submission per 60 s — a batch loses exactly one cell, whose retry then succeeds |
| `demo:drop-first` | job created, response "lost" (502 once per key); the key-reusing retry recovers it through the replay path |

Four `localStorage` switches, separate from the prompt tokens: `MOCK_FAST=1` (sub-second jobs — what the test suite uses), `MOCK_DEMO=1` (one denoise stride per poll, for screen recording), `MOCK_METRICS=empty|stale`, `MOCK_HOUR` (pins the throughput window so bar positions do not drift with the wall clock).

## Testing

Playwright, visual and functional in one suite. **67 tests in 3 files**: `visual.spec.ts` (59 — the regression suite), `capture.spec.ts` (7 — screenshot capture, not assertions), `preview-demo.spec.ts` (1 — a video walkthrough, run via `--config=playwright.video.config.ts`). A bare run executes all three.

```bash
npm run test:visual           # builds the mock bundle, previews it on :4173, runs everything
npm run test:visual:update    # re-record the screenshot baselines
```

The suite drives the MSW mock, so it needs no gateway, no GPU and no network. Coverage spans the three views end to end: submit paths, streaming previews and their crossfade, stage timelines, cancel, 429 shed and countdown, idempotent replay after a dropped response, sweep seed-locking, ledger filters and rerun-seed byte identity, the detail overlay's focus trap and restore, deep links and back/forward, the operate poll loop pausing on tab hide, ARIA progressbar and throttled announcements, and 4.5:1 contrast assertions on the two text tiers. Every test asserts zero console errors.

Screenshots: 20 baselines at 1440×900 plus responsive passes at 375×667 and 768×1024, `maxDiffPixelRatio: 0.002`, animations disabled, `reducedMotion: 'reduce'`, generated images and every `data-testid="dyn"` element masked. Baselines carry Playwright's platform suffix and **the committed set is `-darwin` only** — a Linux CI runner regenerates rather than compares. That is why the suite is not in CI; the functional assertions would pass there, the pixels would not.

## Build and serving

```bash
npm run build        # tsc -b && vite build  ->  dist/
```

In production the frontend is a build stage, not a service. `deploy/stack/Dockerfile` stage 1 is `node:22-slim` running `npm ci && npm run build`; the runtime stage copies `dist/` to `/app/frontend`, and `deploy/stack/serve.py` — an ASGI wrapper in front of the untouched gateway app — routes `/v1`, `/health`, `/docs`, `/redoc` and `/openapi.json` to the gateway and everything else to `StaticFiles(html=True)`. Same origin, no CORS, one port.

`vite.config.ts` adds a `spa-404-fallback` plugin that copies `dist/index.html` to `dist/404.html` after the bundle is written. Starlette's `StaticFiles(html=True)` serves `404.html` — not `index.html` — for an unknown path, so without that copy a `/jobs/:id` deep link returns a bare 404 instead of booting the router. Vite's own dev server and `preview` already fall back on their own; only the deployed stack needs the file.

## Structure

```
src/
  api/            client.ts (fetch, auth header, error envelope parsing,
                  30s timeout), types.ts (the wire contract)
  views/          GenerateView.tsx, GalleryView.tsx (the ledger),
                  OperateView.tsx
  components/     ParamsRail, StageTimeline, BatchTimeline, JobImage
                  (thumbhash + lazy + 410), Lightbox, ErrorEnvelope,
                  Sparkline, ThroughputBars, KeyPopover, HealthDot, Copyable
  hooks/          useBatch.ts (the submit/poll/stage state machine for
                  1..N cells), useCopied.ts
  lib/            router.ts, store.ts (key, health, correlation id, session
                  batches), params.ts (defaults, sweep grids, seed
                  derivation), bench.ts (BENCHMARKS.md constants),
                  imageCache.ts (jobId -> object-URL LRU), thumb.ts,
                  format.ts, download.ts
  mocks/          handlers.ts (the full wire contract + demo scenarios),
                  browser.ts
  index.css       the whole design system: tokens, elevation ladder, type
                  scale, focus and reduced-motion rules
tests/            visual.spec.ts (regression), capture.spec.ts,
                  preview-demo.spec.ts, and the -darwin baselines
public/mock/      preview frames and seed-keyed sample images
scripts/          make-preview-frames.mjs, make-seed-variants.mjs
```

## Accessibility

- Both modals are native `<dialog>` + `showModal()`: focus trap, inert backdrop, Escape via `cancel`. Focus returns to the opener explicitly — the native restore is unreliable when `close()` runs during a React unmount. The lightbox nests inside the detail dialog and stops `cancel` propagating, so Escape closes the topmost one only.
- Two text tiers, both AA at 11px on every surface: values at 0.67 L, labels and captions at 0.63 L (~6:1). A third token is decorative only and never carries information alone. Two Playwright tests assert 4.5:1 on the label and segment tiers.
- Live progress is a `role="progressbar"`; the screen-reader announcement is throttled to stage transitions and 20% strides, never the second-ticking rows. Errors are `role="alert"`.
- Visible focus outline on every interactive element, a skip link, and `prefers-reduced-motion` handled twice: `MotionConfig reducedMotion="user"` keeps opacity crossfades and drops transforms, and a global CSS block collapses every animation and transition to 0.01 ms.

## Not implemented

- **Batch grouping is session-only.** The wire has no batch field, so a batch/sweep set is grouped client-side in memory (a 4-char stem, never sent). The chips say so in their tooltips, and the grouping is gone after a reload.
- **The ledger does not refresh.** One fetch per activation, no polling and no pagination past `limit=100`; a job that completes while the ledger is open appears when the view is re-entered.
- **Snapshot baselines are darwin-only** (see Testing). The design spec called for Linux-container baselines; the committed set is what the development machine produced.
- **The prompt bar clips at 375×667.** In the completed state the textarea's third line runs under the footer — visible in `completed-375x667-darwin.png`. The layout is usable, not correct, at that height.
- **The batch timeline folds `finalize` into `inference`.** The stage is recorded and shown solo, but the aggregate has three rows (`queued | inference | done`).
- **No streaming transport.** Progress is client-side polling. SSE is available on RunPod queue endpoints and is deferred; the RunPod proxy's 100 s Cloudflare cap makes a naive long-lived connection a liability, and polling is unaffected by it.
- **No CI job.** The frontend builds in `make build-stack` (a failing node stage fails the image build), but lint and the Playwright suite are run locally only.
- **Single dark theme.** No light mode and no theme switch — the instrument has one surface.
