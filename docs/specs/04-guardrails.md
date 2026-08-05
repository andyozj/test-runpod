# 04 — Guardrails

Content safety at two checkpoints. `diffusers` FLUX pipelines ship **no** `safety_checker` — unlike the older Stable Diffusion pipelines, there is nothing built in. Whatever is not added here does not exist.

> **Diagram:** guardrail chain — *pending Excalidraw*

## Contract

```python
@dataclass(frozen=True)
class GuardrailVerdict:
    action: Literal["allow", "flag", "block"]
    categories: tuple[str, ...] = ()
    reason: str | None = None
    score: float | None = None

class PromptGuardrail(Protocol):
    async def check(self, prompt: str, ctx: RequestContext) -> GuardrailVerdict: ...

class ImageGuardrail(Protocol):
    async def check(self, image: bytes, ctx: RequestContext) -> GuardrailVerdict: ...
```

Three actions, not two. Moderation has three honest answers — fine, not fine, and unsure — and a binary interface forces every unsure case to be collapsed into one of the other two at the moment of judgement, after which the information is gone.

Guardrails compose: a `ChainedPromptGuardrail` runs members in order and returns the most severe verdict. Adding a classifier later is registering another member, not editing the call site.

### What `flag` does today

**Nothing that `allow` does not, plus a log line and a counter.** The review queue is not built, and there is no flagged job status.

It exists in the contract regardless, because the decision point is what is expensive to retrofit. A guardrail author facing an ambiguous prompt with only allow/block available must pick one, and the ambiguity is never recorded. With `flag` available the verdicts accumulate from day one, so a review queue added later has a history to work from rather than starting empty.

Stated plainly so nobody reads `flag` as implying a workflow that exists.

## Placement

Two checkpoints for prompts, one for images.

| Checkpoint | Tier | Built | Why there |
|---|---|---|---|
| Prompt | gateway | Blocklist | Blocks before spending ~22s of GPU time. This is the chain that grows — a classifier is added here |
| Prompt | worker | **Same blocklist** | The RunPod endpoint is independently reachable by anyone holding the API key. A gateway-only check is bypassable |
| Image | worker | `NoopImageGuardrail` | The only place the pixels exist without paying to move them |

**Both prompt checks are currently identical.** The gateway's is the one intended to grow — it can afford a model call or an external API, because it runs before any GPU is committed. The worker's must stay model-free: it runs on billed GPU time, and loading a classifier there would inflate the image and the cold start.

That divergence is the design, not the current state. Today it is one blocklist in two places, and saying otherwise would oversell it.

The duplication is deliberate. Defence in depth here is not theoretical: the endpoint is a public URL with its own credential, and treating the gateway as the sole entry point assumes an invariant that does not hold.

## Ordering

Where the checks sit relative to everything else, because getting this wrong is how a blocked image ends up at a public URL.

**Gateway `submit()`:**

```
1. guardrail.check(prompt)
2. repository.create(status = BLOCKED if blocked else QUEUED)
      → unique violation? return the existing job
3. if QUEUED: runpod.submit(...)
```

The guardrail runs **before** the insert, so a blocked prompt is still recorded as a job — the caller gets a `job_id` and a `BLOCKED` status rather than a bare error, and the block is attributable and countable. Idempotency still works: a replay returns the existing `BLOCKED` job without re-running anything.

**Worker `handler()`:**

```
1. validate
2. prompt guardrail        → block before touching the GPU
3. generate
4. image guardrail         → BEFORE upload
5. upload to storage
6. return the reference
```

**Step 4 must precede step 5.** Upload first and a blocked image is already sitting at a reachable URL; deleting it afterwards is a race the blocker loses, and the URL may already have been logged. An image that fails the check is never written anywhere.

## What is built

**`BlocklistPromptGuardrail`** — normalised matching against a curated term list.

Normalisation is the whole substance of it. A raw `in` check is defeated by casing, spacing, punctuation, leading/trailing whitespace, and confusable Unicode characters. The implementation applies NFKC normalisation, casefolds, strips combining marks, collapses separator runs, then matches on word boundaries.

It is not a content classifier and does not pretend to be. It stops naive cases and demonstrates that the hook is real and wired through both tiers.

### The term list

`contracts/blocklist.json` at the repository root, alongside the other shared contracts ([02](02-gateway-core.md#the-duplicated-contract)). Both packages load it; neither owns it. Without that, the two tiers drift and the worker blocks a different set than the gateway — which is worse than either list alone, because the gateway's block rate stops describing what actually happens.

```json
{
  "version": 1,
  "categories": {
    "csam": ["<terms>"],
    "graphic_violence": ["gore", "mutilation"],
    "self_harm": ["<terms>"]
  }
}
```

**The committed list is deliberately minimal**, holding only printable category terms. Three reasons, and none of them is squeamishness:

- A repository of curated slurs and abuse terms is content in its own right, and shipping one in a submission invites questions the submission was not meant to raise.
- Hand-maintaining an abuse list is the wrong job. It goes stale, misses regional variants, and any serious deployment sources a maintained list rather than growing its own.
- The interesting engineering is the *matching*, not the terms. A list that is 10 entries or 10,000 exercises identical code.

Production lists load from configuration at deploy, merged over the committed defaults. The format is documented so a maintained source can be dropped in.

**Tests use synthetic tokens** — `zzqblockedqz` and similar. The normalisation suite has to prove it defeats casing, zero-width characters, combining marks and confusable substitution, and a made-up token demonstrates all of that as rigorously as a real one while keeping the test file readable and inoffensive.

**`NoopImageGuardrail`** — returns `allow`, registered and exercised in the request path.

A registered no-op is worth more than an unimplemented interface. It proves the hook is called at the right point with the right data, and it means adding a real classifier changes one binding rather than discovering the extension point was the wrong shape.

## What is not built

| Extension | Integration point | Cost |
|---|---|---|
| CLIP/NSFW image classifier | Replace `NoopImageGuardrail` | ~500MB image growth, a few hundred ms per generation, a false-positive rate to tune |
| LLM prompt classifier | Add a chain member at the gateway | External API dependency and latency on the hot path |
| Prompt-injection detection | Chain member | Only matters once prompts reach a downstream LLM |
| C2PA / invisible watermark | After the image guardrail | Increasingly a regulatory expectation for generative image output |
| Human review queue | Consumes `flag` verdicts | Needs storage, a UI, and someone to staff it |

## Failure policy

If a guardrail raises, the request is **blocked**, not allowed through.

Fail-open is the wrong default for a safety control: a guardrail that silently disables itself when its dependency is down is worse than no guardrail, because the system reports itself as protected. The policy is configurable per guardrail for cases where availability genuinely outranks the check, but the default is closed.

Guardrail latency is bounded by a timeout. A hung check must not hold a request open indefinitely — the timeout expiring counts as a raise, and therefore blocks.

## Observability

Every non-`allow` verdict is logged with `action`, `categories`, `api_key_id`, `correlation_id`, and the prompt preview.

Blocks are counted separately from errors. Conflating them makes the block rate unmeasurable, and the block rate is the only signal that tells you whether the list is too tight or too loose.

`BLOCKED` is a distinct job status for the same reason — see [02](02-gateway-core.md).

## Testing

All of it runs GPU-free.

- Normalisation defeats casing, spacing, punctuation, zero-width characters, and confusable substitution — parametrized, one case per evasion.
- Word-boundary matching does not fire on substrings inside innocent words. This is the false-positive case that makes naive blocklists unusable.
- Chain returns the most severe verdict.
- A raising guardrail blocks; a timing-out guardrail blocks.
- The image hook is invoked with the generated bytes, asserted with a recording fake.
- **A blocked image is never uploaded** — the storage fake records zero writes.
- A blocked prompt never reaches `runpod.submit` — the client fake records zero submissions.
- `BLOCKED` is persisted distinctly from `FAILED`.
- Both packages load `contracts/blocklist.json` and agree on the same verdicts for the same input.

Per [`STANDARDS.md`](../../STANDARDS.md) §9, the error-code contract requires 100% coverage, and guardrail decisions are part of it.
