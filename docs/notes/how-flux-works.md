# How FLUX.1-dev works — training, architecture, and prompt → image

Companion to `full-picture.md`. What the model is, how it was trained, and
exactly what happens between your prompt and the PNG. Where a detail connects
to a decision in this repo, the link is stated.

Black Forest Labs published the architecture and method lineage but never the
dataset or training compute. Everything below is from their releases, the
model card, and the diffusers implementation we actually run. Unpublished
things are marked unpublished.

---

## 1. What the model is

FLUX.1-dev is a **12-billion-parameter rectified-flow transformer** for
text-to-image generation. Released August 2024 by Black Forest Labs (the team
behind the original Stable Diffusion papers).

It is not one network. Four parts run in sequence:

| Part | Size | Job |
|---|---|---|
| CLIP ViT-L/14 text encoder | ~0.1B | Reads the prompt once → one pooled vector: the global "vibe" |
| T5-XXL text encoder | ~4.8B | Reads the prompt once → one embedding per token (up to 512): the fine-grained content |
| **The transformer** (the actual FLUX) | ~12B | The iterative denoiser. All the generation happens here |
| VAE decoder | ~0.1B | Turns the finished latent grid into RGB pixels |

The three FLUX.1 variants share this architecture and differ only in
distillation:

| Variant | What it is | Licence |
|---|---|---|
| pro | The undistilled base. API-only, weights never released | Commercial API |
| **dev** (ours) | **Guidance-distilled** from pro — see §3 | Non-commercial |
| schnell | Additionally **timestep-distilled** to 1-4 steps | Apache-2.0 |

## 2. Training methodology: rectified flow

### The idea

Older diffusion models (Stable Diffusion 1.x/XL) learn to reverse a noising
process along a curved path — the model predicts "what noise was added" and
walks back in many small, curved steps.

FLUX uses **rectified flow** (also called flow matching): draw a **straight
line** between a real image's latent `x₀` and pure noise `ε`:

```
x_t = (1 − t)·x₀ + t·ε        t ∈ [0, 1]
```

and train the network to predict the **velocity** along that line:

```
v(x_t, t) ≈ ε − x₀
```

The loss is plain mean-squared error between predicted and true velocity,
with timesteps sampled from a logit-normal distribution so training
concentrates where the problem is hardest (the middle of the path).

Why it matters: straight paths can be integrated in fewer, larger steps
without falling off the trajectory. This is why 28 steps suffice where SD1.5
needed 50, and why schnell can survive distillation down to 1-4.
It is also why our benchmark sweeps steps 4→50 (`09-benchmarks.md` §2):
the quality-vs-steps curve of a rectified-flow model is a measurable knee,
not folklore.

### What it trains on

The network never sees pixels. Images are first compressed by the VAE encoder
into a 16-channel latent grid at 1/8 resolution — a 1024×1024 image becomes
128×128×16 ≈ 260k numbers instead of 3.1M. Training and inference both happen
in that latent space; that is what makes a 12B denoiser affordable at all.

Resolution-dependent **timestep shifting** is applied at higher resolutions
(more time spent in the high-noise regime, where large images need more work).
This survives into inference — the scheduler shifts its sigma schedule based
on image size, which is one reason latency does not scale linearly with pixels.

Dataset, data filtering, and training compute: **unpublished.**

### Guidance distillation — what makes dev "dev"

Classifier-free guidance (CFG) is how diffusion models follow prompts: run the
denoiser **twice** per step (with prompt, without), then push the result away
from the unconditional one. Effective, but it doubles compute per step.

FLUX.1-pro was trained with real CFG available. FLUX.1-dev was then
**distilled to imitate the guided behaviour in a single pass**: guidance
strength becomes just another conditioning input (an embedding), and the
network learns to produce the output that the two-pass procedure would have
produced.

Consequences you can see in this repo:

- `guidance_scale: 3.5` is **an embedding fed into the model**, not the CFG
  mixing weight it is in SD — which is why sensible values differ from SD's.
- **No negative prompt** (`01-worker.md`). There is no unconditional pass to
  push away from. `diffusers` can force real CFG back on (`true_cfg_scale`),
  but that surrenders the distillation: ~2× latency and cost, two interacting
  guidance mechanisms, no reliable quality gain. The benchmark measures the 2×
  claim rather than repeating it.
- One forward pass per step is the performance baseline our cost model rests on.

## 3. The transformer itself

A **multimodal diffusion transformer (MMDiT lineage, from the SD3 paper)**
with two block types:

- **Double-stream blocks** (19): image tokens and text tokens flow through
  separate weights but attend to each other in **joint attention** — text
  informs image and image informs text at every layer.
- **Single-stream blocks** (38): the two sequences are concatenated and share
  one set of weights — cheaper, and by then the modalities are fused.

Conditioning enters two ways:

- **Sequence conditioning:** the 512 T5 token embeddings sit in the attention
  as first-class tokens.
- **Modulation conditioning:** timestep + pooled CLIP vector + the guidance
  embedding are combined into a vector that scales/shifts every layer's
  activations (adaptive layer norm) — the "global dial" while T5 supplies
  the "what".

Positions use **rotary embeddings (RoPE)** over 2D image coordinates, which is
why the model tolerates arbitrary aspect ratios and resolutions within its
trained range.

Numbers that matter operationally: hidden size 3072, ~12B parameters, bf16 →
**~24GB for the transformer alone**, ~34GB with T5-XXL, CLIP and VAE resident —
the reason `24GB cards cannot run this worker` and we deploy on 48GB L40S
(`01-worker.md`, endpoint config).

## 4. Prompt → final image, step by step

What actually executes when you curl the endpoint. Timings are for 1024²,
28 steps, warm L40S — to be replaced by measured figures from `BENCHMARKS.md`.

```
 your JSON            {"input": {"prompt": "a red fox in falling snow", "seed": 42}}
    │
 1. validation        handler.py — bounds, types, dimension snapping   <1ms
    │                 (1000px → 992px: latent tokens exist every 16px,
    │                  so dimensions snap down to ×16)
 2. prompt guardrail  blocklist check — a blocked prompt costs $0 GPU  <1ms
    │
 3. text encoding     CLIP reads prompt → 1 pooled vector              ~150ms total
    │                 T5-XXL reads prompt → up to 512 token embeddings
    │                 (chars beyond ~512 T5 tokens are silently cut —
    │                  why prompt is capped at 2000 chars)
    │
 4. noise init        seeded RNG → 16-channel latent grid, 128×128     <10ms
    │                 (the seed makes THIS reproducible — same seed,
    │                  same noise, same image)
    │
 5. THE LOOP ×28      each step:                                       ~0.7s/step
    │                   • transformer forward pass: image tokens +      ≈ 20s total
    │                     T5 tokens in joint attention; timestep,
    │                     CLIP vector and guidance embedding modulate
    │                     every layer → predicted velocity
    │                   • Euler step: move the latent a fraction
    │                     along the predicted straight line toward
    │                     the image
    │                   • our callback fires → progress_update →
    │                     polling clients see {"percent": 43}
    │                 ONE pass per step (guidance is distilled in —
    │                 SD-style models pay two)
    │
 6. VAE decode        16-ch latent 128×128 → RGB 1024×1024             ~300ms
    │
 7. encode + return   PNG bytes → base64 → response envelope           ~300ms
    │                 with seed, dimensions, model_version, timings
    ▼
 fox.png              ~2MB PNG, ~2.7MB as base64
```

Two details that answer common "why" questions:

- **Why is step 5 the whole cost?** 28 × (one 12B forward pass). Everything
  else is milliseconds. This is why `num_inference_steps` is the latency dial,
  why cost-per-image is nearly linear in steps, and why the benchmark's
  quality-vs-steps grid can change the default: if 20 steps looks like 28,
  every image gets 30% cheaper.
- **Why is the same seed reproducible but not bit-identical across GPUs?**
  The noise (step 4) is exactly reproducible from the seed. But bf16 kernel
  and cuDNN algorithm selection differ across GPU models, so pixels can drift
  microscopically between hosts. Same GPU type → same image. This is why the
  benchmark compares weight *hashes* across endpoints, never pixels
  (`09-benchmarks.md` §7).

## 5. Cold start anatomy (what happens before step 1 is possible)

Once per worker, not per request:

```
1. host selected            RunPod prefers hosts already holding the cached model
2. image pulled             2.92GB — our code + runtime, no weights
3. weights.resolve()        finds the staged snapshot, verifies it is the
                            PINNED revision, refuses to boot otherwise
4. safetensors → VRAM       ~34GB of bf16 weights load onto the GPU   ~30-60s
5. worker polls the queue   FlashBoot keeps VRAM resident across idle gaps,
                            so the next cold start may skip step 4
```

The load in step 4 is the single biggest latency in the system — and the
reason the handler warms the pipeline at container start (before the first
billed job) rather than lazily on first request.

## 6. The choices in this repo, restated as model facts

| Repo decision | The model fact behind it |
|---|---|
| No negative prompt | dev is guidance-distilled; no unconditional pass exists |
| `guidance_scale` default 3.5 | It's a distilled embedding, not CFG weight — SD intuitions don't transfer |
| Dimensions snap to ×16 | VAE 8× downsampling × 2×2 patchification = one token per 16px |
| Prompt cap 2000 chars | T5 truncates silently at 512 tokens; the cap keeps truncation rare |
| 28-step default | Rectified flow's quality knee; swept 4→50 in the benchmark |
| Seed always echoed | Noise init is the only random input; the seed fully determines it |
| 48GB GPU floor | ~34GB resident in bf16 before activations |
| One job per GPU | A single forward pass saturates the GPU; concurrency adds contention, not throughput |
| Weight hash, not pixel diff, across endpoints | bf16 kernels are not bit-reproducible across hosts |
