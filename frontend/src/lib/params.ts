import type { GenerationRequest } from '../api/types'

export type BatchSize = 1 | 2 | 4
export type Mode = 'batch' | 'sweep'
export type SweepParam = 'steps' | 'guidance' | 'size'

export interface FormParams {
  prompt: string
  width: number
  height: number
  steps: number
  guidance: number
  seed: number | null
  /** dice = new random seed each run; lock = keep the seed */
  seedMode: 'dice' | 'lock'
  format: 'png' | 'jpeg'
  batch: BatchSize
  mode: Mode
  sweep: SweepParam
}

export const DEFAULT_PARAMS: FormParams = {
  prompt: '',
  width: 1024,
  height: 1024,
  steps: 28,
  guidance: 3.5,
  seed: null,
  seedMode: 'dice',
  format: 'png',
  batch: 1,
  mode: 'batch',
  sweep: 'steps',
}

export const SEED_MAX = 2147483647

/** One variable across 4 cells; everything else, seed included, held constant. */
export const SWEEP_VALUES: Record<SweepParam, readonly number[]> = {
  steps: [4, 12, 20, 28],
  guidance: [1, 3.5, 7, 10],
  size: [512, 768, 1024, 1280],
}

export function randomSeed(): number {
  const buf = new Uint32Array(1)
  crypto.getRandomValues(buf)
  return buf[0] & SEED_MAX
}

/** Params for one sweep cell: the swept value applied, all else untouched. */
export function sweepCellParams(p: FormParams, index: number): FormParams {
  const v = SWEEP_VALUES[p.sweep][index]
  switch (p.sweep) {
    case 'steps':
      return { ...p, steps: v }
    case 'guidance':
      return { ...p, guidance: v }
    case 'size':
      return { ...p, width: v, height: v }
  }
}

/** The varied value as a cell label: `4 steps`, `g 3.5`, `768 px`. */
export function sweepValueLabel(sweep: SweepParam, index: number): string {
  const v = SWEEP_VALUES[sweep][index]
  switch (sweep) {
    case 'steps':
      return `${v} steps`
    case 'guidance':
      return `g ${v}`
    case 'size':
      return `${v} px`
  }
}

export function sweepRequests(p: FormParams, seed: number): GenerationRequest[] {
  return SWEEP_VALUES[p.sweep].map((_, i) => toRequest(sweepCellParams(p, i), seed))
}

/**
 * Per-cell seeds. N=1 keeps the single-job contract (dice = server picks).
 * N>1 always chooses client-side so every cell's seed is echoed and reproducible:
 * lock → seed, seed+1, …; dice → independent crypto random ints 0..2^31-1.
 */
export function batchSeeds(p: FormParams, n: number): (number | null)[] {
  if (n === 1) return [p.seedMode === 'lock' ? p.seed : null]
  if (p.seedMode === 'lock' && p.seed !== null) {
    return Array.from({ length: n }, (_, i) => (p.seed! + i) % (SEED_MAX + 1))
  }
  const buf = new Uint32Array(n)
  crypto.getRandomValues(buf)
  return [...buf].map((v) => v & SEED_MAX)
}

export type Prefill = Partial<FormParams>

/** Mock scenario precedence: job-shaping tokens win over transport ones. */
const DEMO_TOKENS = [
  'mixed',
  'blocked',
  'oom',
  'cold',
  'hold',
  'shed-one',
  'shed',
  'drop-first',
] as const

/**
 * The demo:x scenario a prompt selects. The mock gates scenarios on the demo:
 * prefix, and a prompt may carry several — this resolves the same winner.
 */
export function demoScenario(prompt: string): string | null {
  const tokens = new Set(
    [...prompt.matchAll(/demo:([a-z-]+)/g)].map((m) => m[1]),
  )
  return DEMO_TOKENS.find((t) => tokens.has(t)) ?? null
}

export const SIZE_OPTIONS: number[] = []
for (let d = 256; d <= 1536; d += 16) SIZE_OPTIONS.push(d)

export function toRequest(
  p: FormParams,
  seedOverride?: number | null,
): GenerationRequest {
  const seed =
    seedOverride !== undefined
      ? seedOverride
      : p.seedMode === 'lock'
        ? p.seed
        : null
  return {
    prompt: p.prompt,
    width: p.width,
    height: p.height,
    num_inference_steps: p.steps,
    guidance_scale: p.guidance,
    ...(seed !== null ? { seed } : {}),
    output_format: p.format,
  }
}
