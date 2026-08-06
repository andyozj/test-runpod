# 01 — Worker

Tier 1. Runs on RunPod Serverless. This is the graded deliverable.

> **Diagram:** [worker lifecycle, cold vs warm](https://excalidraw.com/#json=kwh-XFq8_sxRUP9eEND3M,7JVRVZVNXn7KKsg89QYG3Q) — opens in Excalidraw, editable

## Modules

| Module | Responsibility |
|---|---|
| `handler.py` | RunPod entrypoint. Parse → guard → delegate → serialise. No inference logic. |
| `pipeline.py` | Lazy accessor and the `ImagePipeline` protocol. |
| `inference.py` | `generate(request, pipeline) -> GenerationResult`. Pure, injectable. |
| `guardrails.py` | Cheap model-free prompt check. See [04](04-guardrails.md). |
| `schemas.py` | `GenerationRequest`, `GenerationResult`, error envelope. |
| `settings.py` | `pydantic-settings`. The only module reading the environment. |
| `errors.py` | Domain exceptions and error codes. |

## The lazy pipeline

Two requirements that a naive implementation cannot satisfy together: the pipeline must load once per worker rather than once per job, and `handler.py` must be importable without a GPU.

```python
_pipeline: ImagePipeline | None = None

def get_pipeline() -> ImagePipeline:
    """Return the process-wide FLUX pipeline, loading it on first use."""
    global _pipeline
    if _pipeline is None:
        _pipeline = _load_pipeline()
    return _pipeline

def set_pipeline(pipeline: ImagePipeline | None) -> None:
    """Replace the process-wide pipeline. Tests only."""
    global _pipeline
    _pipeline = pipeline
```

A module-level constant satisfies the first and breaks the second. The accessor satisfies both.

**Warm-up** happens in `if __name__ == "__main__":`, before `runpod.serverless.start()`. Production workers load during container start rather than during the first billed job; importing the module — which is what tests do — never touches a GPU. No configuration flag: the entrypoint and the import path are simply different.

`ImagePipeline` is a locally-declared `Protocol`, not a direct `FluxPipeline` dependency. `diffusers` is not usefully typed, so under `mypy strict` everything from it arrives as `Any` and trips `warn_return_any` at the first boundary crossing. The protocol is what makes strict mode viable, and it is what tests fake.

## Input contract

| Field | Type | Default | Constraint |
|---|---|---|---|
| `prompt` | `str` | required | non-blank; length bounded by the T5 token limit (below) |
| `width` | `int` | 1024 | 256–1536, snapped down to ×16 |
| `height` | `int` | 1024 | 256–1536, snapped down to ×16 |
| `num_inference_steps` | `int` | 28 | 1–50 |
| `guidance_scale` | `float` | 3.5 | 0–20 |
| `seed` | `int \| None` | `None` | randomised when absent, always echoed |
| `output_format` | `"png" \| "jpeg"` | `"png"` | — |
| `correlation_id` | `str \| None` | `None` | bound into the log context |

### Why there is no negative prompt

Not because the pipeline refuses one — current `FluxPipeline.__call__` accepts `negative_prompt`, `negative_prompt_2`, and `true_cfg_scale`. The reason is cost and coherence.

Standard Stable Diffusion performs classifier-free guidance by running **two forward passes per step**, one conditioned on the prompt and one on the negative, then extrapolating away from the negative. FLUX.1-dev is **guidance-distilled**: it was trained to internalise that behaviour, takes `guidance_scale` as an embedding input, and runs **one pass per step**. The distillation is the source of its speed, and it leaves no unconditional pass for a negative prompt to act against.

Setting `true_cfg_scale > 1` restores real CFG, and with it:

- **Roughly 2× latency and 2× cost** — the distillation speedup is surrendered entirely.
- **Two interacting guidance controls.** The distilled `guidance_scale` embedding and real `true_cfg_scale` operate at once. They are not independent and there is no principled way to tune both.
- **No reliable quality gain.** The model was not trained to be sampled with real CFG at inference; output can degrade rather than improve.

So the field is omitted, and this is the reason. Adding it later is a schema change and a pass-through, not a redesign.

[09](09-benchmarks.md) measures the 2× claim rather than repeating it.

### Prompt length

Non-blank, capped at 2000 characters.

FLUX runs T5 at `max_sequence_length=512` tokens. A character cap is a proxy for that, not a substitute: an unusually dense 2000-character prompt can still exceed 512 tokens, and `diffusers` truncates the excess silently.

Validating exactly would mean loading the T5 tokenizer, which is a dependency to inject and fake in tests for a case that is rare in practice. For this scope the character cap plus a documented note is the right trade. The limitation is recorded in [08](08-production-readiness.md) rather than hidden.

### Dimension snapping

Dimensions snap down to a multiple of 16 — the FLUX latent space is 16× downsampled — and the effective values are returned on the result. Recent `diffusers` already rounds and warns; verify at implementation time and delegate rather than duplicate.

Snapping is silent by design, unlike prompt truncation: the difference between 1000px and 992px is invisible, whereas the difference between a full prompt and half of one is the whole request.

## Output contract

```json
{
  "image_base64": "iVBORw0KGgoAAAANS...",
  "format": "png",
  "seed": 42,
  "width": 1024, "height": 1024,
  "num_inference_steps": 28, "guidance_scale": 3.5,
  "model_version": "black-forest-labs/FLUX.1-dev@<revision>",
  "timings": {"inference_s": 21.4, "encode_s": 0.3}
}
```

**Base64 is the default, and the platform decides it.**

RunPod serverless exposes a fixed surface — `/run`, `/runsync`, `/status/{job_id}`, `/stream`, `/cancel`, `/retry`, `/health`. Custom routes cannot be added. So `GET /status/{job_id}` returns **the handler's return value verbatim**, and there is nowhere else a caller can obtain a result from.

Results are retained 30 minutes after completion for `/run` and 1 minute for `/runsync`. Any poller — the client, the gateway reconciler — must fetch within that window; after it, `/status` has nothing to return.

That makes the rule simple: whatever the handler does not put in its output does not exist as far as a direct caller is concerned. A storage key in the output hands the reviewer a key they cannot resolve, because resolving it requires storage credentials or a gateway they are not running. The brief asks the endpoint to *return a generated image*; only base64 satisfies that unconditionally.

At 1024² PNG this is ~2MB, ~2.7MB encoded. Request caps are documented — 10MB for `/run`, 20MB for `/runsync` — but no ceiling is documented for the `/status` response, so it is measured in [09](09-benchmarks.md) at 1536², with JPEG as the fallback if it binds.

### The demonstration is one `/runsync` call

The case study's test criterion is *"accept a text input and return a generated image."* `/runsync` holds the connection and returns the finished output in one response, so the demonstration is a single command:

```bash
curl -s -X POST "https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/runsync" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input": {"prompt": "a red fox in falling snow"}}' \
  | jq -r '.output.image_base64' | base64 -d > fox.png
```

This works because a warm job finishes in ~20-25s and 2.7MB fits the 20MB `/runsync` cap. It needs a warm worker — a cold start would outlast the hold — which is one reason the demo window runs with an active worker. It is the demo path only; everything about async as the service interface ([00](00-overview.md#async-as-the-default)) stands.


## Errors

Codes are defined in [02](02-gateway-core.md#error-codes) and shared across tiers.

**The envelope travels as a JSON string in the job's `error` field.** The SDK pops an `error` key from the handler's return and forwards it, but the platform carries error only as a string — a dict there is silently dropped, leaving a `COMPLETED` job with no output at all. Discovered by the live e2e suite (2026-08-06), which is the class of bug only e2e can find. So `WorkerError.job_output()` JSON-encodes the envelope; callers decode `error` to recover code, message and suggestion; the job reports `FAILED`. The gateway adapter decodes string or dict and treats an undecodable error as message-only rather than raising — a malformed error must never mask the failure it reports.

`torch.cuda.OutOfMemoryError` is caught explicitly: log with context, `torch.cuda.empty_cache()`, return the envelope with `refresh_worker: True`. VRAM fragmentation outlives `empty_cache()`, so the worker is retired rather than trusted with the next job.

Any other pipeline exception maps to `INFERENCE_FAILED` with the detail logged and not returned.

## Endpoint configuration

| Setting | Value | Reason |
|---|---|---|
| GPU | L40S 48GB | bf16 weights are ~34GB resident (23.8GB transformer + ~9.5GB T5-XXL + CLIP + VAE), plus activations. A 24GB card cannot hold the model; 48GB is the floor with headroom. |
| GPU priority | L40S → A100 80GB | An endpoint accepts up to three GPU types in priority order. The fallback keeps the demo alive when L40S is scarce; benchmark runs pin a single type so the numbers stay comparable. |
| Workers | min 0, max 3 | Scale to zero. 3 demonstrates concurrency. **min 1 during the demo window** — an active worker eliminates the cold start and is what makes `/runsync` viable. Billed continuously; set back to 0 after. |
| FlashBoot | on | Keeps VRAM resident between jobs. |
| Idle timeout | 60s | Long enough that a demo sequence stays warm. |
| Execution timeout | 300s | Above worst-case 50-step 1536². |
| Job TTL | default (24h) | If it expires mid-run the job vanishes and `/status` returns 404 — the default is far above any real job here. |
| `concurrency_modifier` | 1 | GPU-bound; a second concurrent job only causes VRAM contention. |

**Provisional.** L40S is the reasoned starting point. The final recommendation comes from measured Phase 2b numbers across at least L40S and A100 80GB.

## Open items

| Item | Resolved in |
|---|---|
| Does current `diffusers` already snap dimensions? | Phase 1, by reading the installed source |
| Cold start, warm latency, cost per image | Phase 2b, measured |
| Cost of `true_cfg_scale > 1` versus the 2× estimate | Phase 2b, measured |

## Progress reporting

Owning the image means the worker is not a black box for 20-25 seconds. `diffusers` fires `callback_on_step_end` after each denoising step; `runpod.serverless.progress_update(job, ...)` writes into the job record mid-execution, where `GET /status/{id}` can read it while the job is still `IN_PROGRESS`.

```python
def _on_step(pipe, step: int, timestep: int, kwargs: dict[str, Any]) -> dict[str, Any]:
    runpod.serverless.progress_update(
        job, {"step": step + 1, "total": total_steps,
              "percent": round(100 * (step + 1) / total_steps)}
    )
    return kwargs
```

Without this, every poll during generation returns `IN_PROGRESS` and nothing else — the client cannot distinguish a job three seconds in from one about to finish, and cannot show a progress bar or estimate anything.

It also changes what the downstream facades are worth. A stream carrying one completion event is decorative; a stream carrying live progress is the reason SSE exists ([03](03-facades.md)).

Progress is throttled to every 10 percentage points (first and final step always report), because the SDK's `progress_update` spawns a thread, an event loop, and a TLS session per call — per-step reporting costs 28 of each per image for granularity no polling client can observe. The callback itself runs between steps on the GPU thread, so the decision to skip must stay trivial, and it is: one integer comparison.

### Not built yet: preview images — designed, deferred to post-deploy

The `/stream/{job_id}` endpoint plus a generator handler could yield intermediate images — the Midjourney-style progressive reveal. It is the first post-deploy stretch item, and the design is settled so building it is execution:

The structural obstacle: `pipe(...)` is a blocking call and `callback_on_step_end` fires inside it, so **a generator handler cannot yield from the callback** — `yield` is bound to the generator's own frame. The known fix is three pieces:

1. **Pipeline on a worker thread**; the callback `put`s events into a `queue.Queue`; the now-unblocked generator drains it and yields. The thread runner belongs in `inference.py` — the handler stays a thin generator. Sentinel for completion, cross-thread exception propagation, cancellation handling.
2. **`taef1` for intermediate decodes, never the real VAE.** The tiny distilled autoencoder for FLUX (~5MB, ~200MB VRAM) decodes a preview in ~1-3ms versus 50-150ms — 28 previews cost <0.5% of a 20s job instead of ~5-10%. The final image still uses the real VAE. Pin its revision like every other weight.
3. **`return_aggregate_stream=True`** in `runpod.serverless.start`, so `/run` + `/status` behave exactly as today and `/stream` is additive.

Parameters when built: `"preview": true` input flag defaulting to off so the cost stays opt-in; previews every ~4th step starting near 40% (earlier latents decode to noise regardless of decoder); ~256px JPEG ~20KB per event. Client side is `for event in endpoint.stream(job)`.

`progress_update` has no such constraint — an ordinary call, invocable from anywhere — which is why it is the version that ships first, and why the stream carries previews rather than duplicating percentages.

## Weight path

`weights.resolve()` tries `WEIGHTS_PATH` first, then RunPod's model cache at `/runpod-volume/huggingface-cache/hub/models--{org}--{name}/snapshots/{revision}/` ([06](06-build-deploy.md)). The worker does not know which deployment variant it is running under — one code path, and a deployment selects a mechanism by configuration alone.

An explicit path always wins, because a deployment that sets one has made a decision the cache must not override. That branch serves the baked image, and would serve a mounted network volume too, though none is deployed.

Startup fails fast if `WEIGHTS_PATH` does not exist. Without that check a misconfigured volume mount falls through to downloading 33GB from HuggingFace on every cold start, which presents as "slow" rather than "broken" and can survive a whole benchmark run undetected.
