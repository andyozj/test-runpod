import { useEffect } from 'react'
import { useCopied } from '../hooks/useCopied'
import {
  SIZE_OPTIONS,
  SWEEP_VALUES,
  randomSeed,
  type BatchSize,
  type FormParams,
  type Mode,
  type SweepParam,
} from '../lib/params'
import { useAppState } from '../lib/store'
import { shortSha } from '../lib/format'
import { HealthDot } from './HealthDot'
import { Sparkline } from './Sparkline'

/** One-line repo-sourced fact under a control; teaching copy, not marketing. */
function Hint({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[11px] leading-snug text-ink-dim">{children}</p>
  )
}

function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-2 border-b border-hairline px-4 py-3.5 last:border-b-0">
      <span className="microlabel">{label}</span>
      {children}
      {hint && <Hint>{hint}</Hint>}
    </div>
  )
}

const inputCls =
  'w-full rounded border border-hairline-strong bg-raised px-2 py-1 font-mono text-[13px] text-ink hover:border-[oklch(1_0_0/24%)]'

function SliderField({
  id,
  label,
  ariaLabel,
  min,
  max,
  step,
  value,
  display,
  hint,
  sweptDisplay = null,
  onChange,
}: {
  id: string
  label: string
  ariaLabel: string
  min: number
  max: number
  step: number
  value: number
  display: string
  hint?: string
  /** sweep owns this param: control disabled, display shows the swept range */
  sweptDisplay?: string | null
  onChange: (next: number) => void
}) {
  const fill = ((value - min) / (max - min)) * 100
  return (
    <div className="flex flex-col gap-2 border-b border-hairline px-4 py-3.5 last:border-b-0">
      <div className="flex items-baseline justify-between">
        <span className="microlabel">{label}</span>
        <span
          className={`font-mono text-[13px] ${sweptDisplay ? 'text-accent' : 'text-ink'}`}
        >
          {sweptDisplay ?? display}
        </span>
      </div>
      <input
        id={id}
        type="range"
        aria-label={ariaLabel}
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={sweptDisplay !== null && sweptDisplay !== undefined}
        onChange={(e) => onChange(Number(e.target.value))}
        className="slider disabled:opacity-40"
        style={{ '--fill': `${fill}%` } as React.CSSProperties}
      />
      {hint && <Hint>{hint}</Hint>}
    </div>
  )
}

function SegmentGroup<T extends string>({
  label,
  options,
  value,
  onSelect,
  titles,
  id,
}: {
  label: string
  options: readonly T[]
  value: T
  onSelect: (next: T) => void
  titles?: Partial<Record<T, string>>
  id?: string
}) {
  return (
    <div
      id={id}
      className="flex rounded border border-hairline-strong bg-raised"
      role="group"
      aria-label={label}
    >
      {options.map((option) => (
        <button
          key={option}
          type="button"
          aria-pressed={value === option}
          title={titles?.[option]}
          onClick={() => onSelect(option)}
          className={`flex-1 cursor-pointer rounded px-2 py-1 font-mono text-[11px] ${
            value === option
              ? 'bg-surface text-accent inset-ring inset-ring-hairline-strong'
              : 'text-ink-dim hover:text-ink'
          }`}
        >
          {option}
        </button>
      ))}
    </div>
  )
}

function CopySeedGlyph({ seed }: { seed: number }) {
  const [copied, copy] = useCopied()
  return (
    <button
      type="button"
      title={`Copy seed ${seed}`}
      aria-label={`Copy seed ${seed}`}
      onClick={() => copy(String(seed))}
      className="absolute right-1 top-1/2 -translate-y-1/2 cursor-pointer rounded bg-raised px-1 py-0.5 font-mono text-[11px] text-ink-dim opacity-0 hover:text-ink focus-visible:opacity-100 group-hover:opacity-100"
    >
      {copied ? (
        'copied'
      ) : (
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
          <rect x="0.5" y="0.5" width="6" height="6" fill="none" stroke="currentColor" />
          <rect x="3.5" y="3.5" width="6" height="6" fill="var(--color-raised)" stroke="currentColor" />
        </svg>
      )}
    </button>
  )
}

function Readout({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between px-4 py-1.5">
      <span className="microlabel">{label}</span>
      <span className="flex items-center gap-1.5 font-mono text-[13px] text-ink">
        {children}
      </span>
    </div>
  )
}

export function ParamsRail({
  params,
  onChange,
  jobsActive = 0,
  mobileOpen = false,
  onClose = () => {},
}: {
  params: FormParams
  onChange: (next: FormParams) => void
  jobsActive?: number
  /** below lg the rail hides; the header "params" button opens it as a drawer */
  mobileOpen?: boolean
  onClose?: () => void
}) {
  const { gatewayOk, modelVersion, wallTimes } = useAppState()
  const sha = shortSha(modelVersion)
  const set = (patch: Partial<FormParams>) => onChange({ ...params, ...patch })
  const swept = params.mode === 'sweep' ? params.sweep : null

  useEffect(() => {
    if (!mobileOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [mobileOpen, onClose])

  return (
    <>
      {mobileOpen && (
        <button
          type="button"
          aria-label="Close parameters"
          onClick={onClose}
          className="fixed inset-0 z-30 cursor-default bg-bg/60 lg:hidden"
        />
      )}
      <aside
        id="params-rail"
        className={`w-76 shrink-0 flex-col gap-3 overflow-hidden border-l border-hairline p-3 ${
          mobileOpen
            ? 'fixed inset-y-0 right-0 z-40 flex max-w-[85vw] bg-bg lg:static lg:z-auto lg:max-w-none lg:bg-transparent'
            : 'hidden lg:flex'
        }`}
      >
      {/* the params card is content-height and floats on the canvas (FLORA);
          stretching it to the rail height left a 125-330px void above ENDPOINT */}
      <div className="panel flex min-h-0 flex-col overflow-y-auto rounded-[6px] border border-hairline">
        <Field label="model">
          <span className="font-mono text-[13px] text-ink">FLUX.1-dev</span>
        </Field>

        <Field label="size" hint="latents are 16× downsampled; dims snap to ×16">
          {swept === 'size' ? (
            <span className="font-mono text-[13px] text-accent">
              512 → 1280 px²
            </span>
          ) : (
            <div className="flex items-center gap-1.5">
              <select
                id="rail-size"
                aria-label="Width"
                className={`${inputCls} select`}
                value={params.width}
                onChange={(e) => set({ width: Number(e.target.value) })}
              >
                {SIZE_OPTIONS.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
              <span className="font-mono text-[13px] text-ink-faint">×</span>
              <select
                aria-label="Height"
                className={`${inputCls} select`}
                value={params.height}
                onChange={(e) => set({ height: Number(e.target.value) })}
              >
                {SIZE_OPTIONS.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
              <span className="font-mono text-[11px] text-ink-label">px</span>
            </div>
          )}
        </Field>

        <SliderField
          id="rail-steps"
          label="steps"
          ariaLabel="Inference steps"
          min={1}
          max={50}
          step={1}
          value={params.steps}
          display={String(params.steps)}
          sweptDisplay={swept === 'steps' ? '4 → 28' : null}
          hint="more denoising passes: sharper detail, linear time cost"
          onChange={(steps) => set({ steps })}
        />

        <SliderField
          id="rail-guidance"
          label="guidance"
          ariaLabel="Guidance scale"
          min={0}
          max={20}
          step={0.1}
          value={params.guidance}
          display={params.guidance.toFixed(1)}
          sweptDisplay={swept === 'guidance' ? '1 → 10' : null}
          hint="prompt adherence vs variety — guidance-distilled: an embedding input, no negative prompt"
          onChange={(guidance) => set({ guidance: Math.round(guidance * 10) / 10 })}
        />

        <Field label="seed">
          <div className="flex items-center gap-1.5">
            <div className="group relative flex-1">
              <input
                id="rail-seed"
                aria-label="Seed"
                type="number"
                min={0}
                max={2147483647}
                className={`${inputCls} disabled:text-ink-faint`}
                disabled={params.seedMode === 'dice'}
                placeholder={params.seedMode === 'dice' ? 'random each run' : 'seed'}
                value={params.seed ?? ''}
                onChange={(e) =>
                  set({
                    seed: e.target.value === '' ? null : clamp(Number(e.target.value), 0, 2147483647),
                  })
                }
              />
              {params.seed !== null && <CopySeedGlyph seed={params.seed} />}
            </div>
            <SegmentGroup
              id="rail-seed-mode"
              label="Seed mode"
              options={['dice', 'lock'] as const}
              value={params.seedMode}
              onSelect={(seedMode) => set({ seedMode })}
              titles={{
                dice: 'New random seed each run',
                lock: 'Keep this seed',
              }}
            />
          </div>
          <Hint>same seed + params = identical image, bit for bit</Hint>
        </Field>

        <Field label="mode">
          <SegmentGroup
            id="rail-mode"
            label="Mode"
            options={['batch', 'sweep'] as const}
            value={params.mode}
            onSelect={(mode) => set(enterMode(params, mode))}
            titles={{
              batch: 'Parallel cells, seeds vary',
              sweep: 'Four cells, one parameter varies, seed locked',
            }}
          />
          {params.mode === 'batch' ? (
            <SegmentGroup
              id="rail-batch"
              label="Batch size"
              options={['1', '2', '4'] as const}
              value={String(params.batch) as '1' | '2' | '4'}
              onSelect={(v) => set({ batch: Number(v) as BatchSize })}
              titles={batchTitles(params)}
            />
          ) : (
            <>
              <select
                id="rail-sweep"
                aria-label="Sweep parameter"
                className={`${inputCls} select`}
                value={params.sweep}
                onChange={(e) => set({ sweep: e.target.value as SweepParam })}
              >
                {(Object.keys(SWEEP_VALUES) as SweepParam[]).map((k) => (
                  <option key={k} value={k}>
                    {k} {SWEEP_VALUES[k].join('/')}
                  </option>
                ))}
              </select>
              <Hint>sweep locks the seed — one variable at a time</Hint>
            </>
          )}
        </Field>

        <Field label="format">
          <SegmentGroup
            id="rail-format"
            label="Output format"
            options={['png', 'jpeg'] as const}
            value={params.format}
            onSelect={(format) => set({ format })}
          />
        </Field>

      </div>

      {/* endpoint + session pin to the bottom of the rail behind their own
          hairline: two intentional blocks, not one stretched panel */}
      <div className="panel mt-auto shrink-0 rounded-[6px] border border-hairline">
        <section className="pb-2.5" aria-label="Endpoint readouts">
          <h2 className="microlabel px-4 pb-1 pt-3">endpoint</h2>
          <Readout label="jobs active">
            <span>{jobsActive}</span>
          </Readout>
          <Readout label="gateway">
            <HealthDot healthy={gatewayOk} />
            <span>{gatewayOk === null ? '—' : gatewayOk ? 'ok' : 'err'}</span>
          </Readout>
          <Readout label="model">
            {sha ? (
              <span title={modelVersion ?? undefined}>@{sha}</span>
            ) : (
              <span>—</span>
            )}
          </Readout>
        </section>

        <section className="border-t border-hairline px-4 pb-3.5 pt-3" aria-label="Session wall times">
          <h2 className="microlabel pb-2">session wall times</h2>
          <Sparkline wallTimes={wallTimes} />
        </section>
      </div>
      </aside>
    </>
  )
}

/** Entering sweep with dice active locks a fresh random seed, visibly. */
function enterMode(params: FormParams, mode: Mode): Partial<FormParams> {
  if (mode === 'sweep' && (params.seedMode === 'dice' || params.seed === null)) {
    return { mode, seedMode: 'lock', seed: randomSeed() }
  }
  return { mode }
}

/** Seed semantics per batch size, stated where the choice is made. */
function batchTitles(params: FormParams): Record<'1' | '2' | '4', string> {
  const locked = params.seedMode === 'lock' && params.seed !== null
  const seedNote = (n: number) =>
    locked
      ? `seeds ${params.seed}${n > 1 ? `, +1${n > 2 ? ', +2, +3' : ''} — deterministic` : ''}`
      : 'independent random seed per cell'
  return {
    '1': 'one generation',
    '2': `two parallel cells · ${seedNote(2)}`,
    '4': `four parallel cells · ${seedNote(4)}`,
  }
}

function clamp(value: number, min: number, max: number): number {
  if (Number.isNaN(value)) return min
  return Math.min(max, Math.max(min, value))
}
