# Where we are — plain-English summary

2026-08-05, evening. Everything below is tested unless marked otherwise.

> **2026-08-06 update:** deployed. Image pushed (`0.1.0-44c9643-slim`, 2.9GB),
> endpoint `7jrg4nu4b47fsv` created via the REST API, cached-model staging in
> progress. Deploy-day findings live in `docs/RUNBOOK.md` (GHCR classic PAT,
> package visibility, console-only Model field, the all-unhealthy signature).
> First verified generation pending; then benchmarks.

## The goal

RunPod's case study: deploy FLUX.1-dev (a text-to-image model) as a serverless
endpoint. Send it a prompt, get back an image. Graded on: platform usage,
handler + model integration, a working deployment, clear docs.

## Status in one sentence

All code is written, tested, and proven inside the real Docker image — the only
things left need money on the RunPod account.

## How it works

```
you ──prompt──> RunPod endpoint ──> queue ──> worker (our image)
                                                │  loads FLUX weights
                                                │  generates the image
you <──image (base64)── RunPod  <──────────────┘
```

- **Our image** (2.92GB) contains the code and Python runtime. No model inside.
- **The weights** (33GB) never enter the image. RunPod's "cached models"
  feature pre-loads them onto host machines before our worker starts. We don't
  pay for that download and cold starts drop to seconds.
- **The worker** finds weights wherever they are: an explicit path, a network
  volume, or the model cache. Same image for every option — only endpoint
  settings differ.

## Decisions made today, and why

| Decision | Why |
|---|---|
| Queue endpoint, not load-balancer | Jobs take 20-25s. A queue absorbs bursts and cold starts; an open HTTP connection times out. Also: the brief literally grades `handler.py`, which only the queue type has |
| Cached models, not baked-in weights | A 45GB image probably can't even be pushed to GHCR (registry layer caps), pulls slowly on every scale-up, and RunPod's docs recommend the cache for HuggingFace models. The volume endpoint stays as a fallback — same image, no rebuild |
| No more Pods anywhere | The Pod was only ever a build machine for the 45GB image. At 2.92GB we build and push from this Mac |
| Tiny base image (`ubuntu:22.04`) | The torch wheel carries its own CUDA libraries. The official CUDA base duplicated 6.6GB nothing would ever load. 11.9GB → 2.92GB |
| Refuse to start on wrong weights | If the cache holds a different model version than we pinned, the worker refuses to boot and says what it found. Better a loud failure than images attributed to the wrong model |
| Demo = one `/runsync` curl | One command in, one image out. That is literally the grading test |

## What is proven, with the proof

| Claim | Proof |
|---|---|
| Code is correct | 137 tests, 91-96% coverage, zero GPU needed |
| The image builds | Built on this Mac, 3 times today, amd64 |
| The image runs our code | Ran the real handler inside it — correct error envelopes |
| Deps are exact | Installed from `uv.lock` (130 pins) — same build every time |
| Weight lookup works | Resolver tried inside the image — clean, explanatory failure when nothing is mounted |
| Gated HF repo reachable | `make weights-check` — verifies without downloading |

Five bugs were found and fixed today that would each have burned an hour of
paid GPU time: two wrong paths in the Dockerfile, two path bugs in the code,
and Ubuntu's broken Python 3.11 package (it ships a release candidate that
crashes torch).

## What's left — in order

| # | Step | Who | Time |
|---|---|---|---|
| 1 | Money on the account (credits from hailong, or ~$20 self-fund) | You | minutes |
| 2 | HF token with FLUX licence accepted + GHCR login | You | minutes |
| 3 | Push image from this Mac | Us | ~15 min |
| 4 | Create endpoint (config files are already written) | Us | minutes |
| 5 | One curl → first image → commit samples | Us | minutes |
| 6 | Benchmark run → `BENCHMARKS.md` | Us | 1-2 hours |
| 7 | Endpoint ID into README → submit | Us | minutes |

## What could still bite

| Risk | Plan |
|---|---|
| Our torch needs newer GPU drivers (CUDA 13) | First deploy answers it. Fix A: endpoint's "Allowed CUDA Versions" filter. Fix B: repin torch to CUDA-12 wheels — one commit |
| Cached models is a beta feature | Volume endpoint is the fallback. Same image, config-only switch |
| PNG response too big at max resolution | Measured in the benchmark; JPEG is the fallback |

## The demo, when live

```bash
curl -s -X POST "https://api.runpod.ai/v2/$ENDPOINT_ID/runsync" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input": {"prompt": "a red fox in falling snow"}}' \
  | jq -r '.output.image_base64' | base64 -d > fox.png
```

~25 seconds, one image, reproducible with the seed it returns.
