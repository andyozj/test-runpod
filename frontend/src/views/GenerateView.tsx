import { useEffect, useRef, useState } from 'react'
import { m } from 'motion/react'
import {
  COLD_QUEUE_MS,
  IDLE_RUN,
  inferenceMsOf,
  queuedMsOf,
  useBatch,
  type JobRun,
  type PreviewFrame,
} from '../hooks/useBatch'
import {
  DEFAULT_PARAMS,
  SEED_MAX,
  SWEEP_VALUES,
  batchSeeds,
  demoScenario,
  randomSeed,
  sweepRequests,
  sweepValueLabel,
  toRequest,
  type FormParams,
  type Prefill,
  type SweepParam,
} from '../lib/params'
import { BENCH_WARM_EXEC_P50_S, COLD_TOOLTIP, generateLabel } from '../lib/bench'
import { fmtS } from '../lib/format'
import { useAppState } from '../lib/store'
import { ParamsRail } from '../components/ParamsRail'
import { StageTimeline } from '../components/StageTimeline'
import { BatchTimeline } from '../components/BatchTimeline'
import { ErrorEnvelope, type EnvelopeData } from '../components/ErrorEnvelope'
import { JobImage } from '../components/JobImage'
import { Lightbox } from '../components/Lightbox'
import { NoKeyState } from '../components/KeyPopover'
import { CopyValue } from '../components/Copyable'
import { downloadUrl, imageFilename } from '../lib/download'

export function GenerateView({
  prefill,
  prefillNonce,
  railOpen = false,
  onCloseRail = () => {},
}: {
  prefill: Prefill | null
  prefillNonce: number
  /** below lg the rail is a drawer toggled from the header */
  railOpen?: boolean
  onCloseRail?: () => void
}) {
  const [params, setParams] = useState<FormParams>(DEFAULT_PARAMS)
  // sweep parameter captured at submit; the rail can change without relabeling cells
  const [activeSweep, setActiveSweep] = useState<SweepParam | null>(null)
  const { runs, submitBatch, cancelAll, retryCell, reset, batchWallMs } =
    useBatch()
  const promptRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (prefillNonce === 0 || !prefill) return
    setParams((prev) => ({ ...prev, ...prefill }))
    setActiveSweep(null)
    reset()
  }, [prefill, prefillNonce, reset])

  // the guided first experiment: a steps sweep on a locked random seed
  const runStepsExperiment = () => {
    const prompt = params.prompt.trim() || EXAMPLE_PROMPTS[0]
    const seed =
      params.seedMode === 'lock' && params.seed !== null ? params.seed : randomSeed()
    const next: FormParams = {
      ...params,
      prompt,
      mode: 'sweep',
      sweep: 'steps',
      seedMode: 'lock',
      seed,
    }
    setParams(next)
    setActiveSweep('steps')
    void submitBatch(
      sweepRequests(next, seed),
      SWEEP_VALUES.steps.map((_, i) => sweepValueLabel('steps', i)),
    )
  }

  const adoptSeed = (seed: number) =>
    setParams((prev) => ({ ...prev, seedMode: 'lock', seed }))

  const generate = () => {
    if (params.mode === 'sweep') {
      // the sweep contract: one locked seed across all four cells
      let seed = params.seedMode === 'lock' ? params.seed : null
      if (seed === null) {
        seed = randomSeed()
        setParams((prev) => ({ ...prev, seedMode: 'lock', seed }))
      }
      setActiveSweep(params.sweep)
      void submitBatch(
        sweepRequests(params, seed),
        SWEEP_VALUES[params.sweep].map((_, i) => sweepValueLabel(params.sweep, i)),
      )
      return
    }
    setActiveSweep(null)
    const seeds = batchSeeds(params, params.batch)
    void submitBatch(seeds.map((seed) => toRequest(params, seed)))
  }

  const editPrompt = () => {
    reset()
    promptRef.current?.focus()
  }

  const run = runs[0] ?? IDLE_RUN
  const isBatch = runs.length > 1
  const jobsActive = runs.filter(
    (r) => r.phase === 'submitting' || r.phase === 'running',
  ).length
  const busy = jobsActive > 0
  const canGenerate = !busy && params.prompt.trim().length > 0
  const envelope = toEnvelope(run)
  const lockedSeed = params.seedMode === 'lock' ? params.seed : null

  // last preview survives the run→done flip just long enough for the handoff crossfade
  const handoffRef = useRef<string | null>(null)
  if (!isBatch) {
    if (run.preview) handoffRef.current = run.preview.src
    else if (run.phase === 'idle' || run.phase === 'submitting')
      handoffRef.current = null
  }

  const { key } = useAppState()
  if (!key) {
    // pre-key: the whole instrument stays visible; only submission is gated
    return (
      <div className="flex h-full min-h-0">
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 overflow-auto">
            <NoKeyState />
          </div>
          <div className="prompt-wrap flex w-full shrink-0 flex-col gap-2 px-6 pb-5">
            <PromptBar
              value={params.prompt}
              onChange={(prompt) => setParams((prev) => ({ ...prev, prompt }))}
              onGenerate={() => {}}
              busy={false}
              canGenerate={false}
              disabled
              promptRef={promptRef}
              params={params}
            />
          </div>
        </div>
        <ParamsRail
          params={params}
          onChange={setParams}
          mobileOpen={railOpen}
          onClose={onCloseRail}
        />
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="relative flex min-h-0 flex-1 overflow-auto px-4 py-6 sm:px-8">
          <Crosshair />
          <div className="m-auto flex w-full justify-center">
            {isBatch ? (
              <BatchCanvas
                runs={runs}
                params={params}
                sweep={activeSweep}
                lockedSeed={lockedSeed}
                batchWallMs={batchWallMs}
                onRetryCell={retryCell}
                onEditPrompt={editPrompt}
                onCancelAll={cancelAll}
                onAdoptSeed={adoptSeed}
              />
            ) : runs.length === 0 && (params.mode === 'sweep' || params.batch > 1) ? (
              <EmptyBatch
                params={params}
                sweep={params.mode === 'sweep' ? params.sweep : null}
                lockedSeed={lockedSeed}
                onPrompt={(prompt) => {
                  setParams((prev) => ({ ...prev, prompt }))
                  promptRef.current?.focus()
                }}
              />
            ) : busy ? (
              <GeneratingCell
                run={run}
                params={params}
                lockedSeed={lockedSeed}
                onCancel={cancelAll}
              />
            ) : envelope ? (
              <ErrorEnvelope
                error={envelope}
                onRetry={() => retryCell(0)}
                onEditPrompt={editPrompt}
              />
            ) : run.phase === 'done' && run.job?.status === 'COMPLETED' && run.job.result ? (
              <CompletedCell
                run={run}
                handoffSrc={handoffRef.current}
                onAdoptSeed={adoptSeed}
              />
            ) : (
              <EmptyCell
                width={params.width}
                height={params.height}
                lockedSeed={lockedSeed}
                onPrompt={(prompt) => {
                  setParams((prev) => ({ ...prev, prompt }))
                  promptRef.current?.focus()
                }}
                onRunExperiment={runStepsExperiment}
              />
            )}
          </div>
        </div>
        <div className="prompt-wrap flex w-full shrink-0 flex-col gap-2 px-6 pb-5">
          <PromptBar
            value={params.prompt}
            onChange={(prompt) => setParams((prev) => ({ ...prev, prompt }))}
            onGenerate={generate}
            busy={busy}
            canGenerate={canGenerate}
            promptRef={promptRef}
            params={params}
          />
        </div>
      </div>
      <ParamsRail
        params={params}
        onChange={setParams}
        jobsActive={jobsActive}
        mobileOpen={railOpen}
        onClose={onCloseRail}
      />
    </div>
  )
}

/** Bench registration mark behind the cell: two hairline strokes, no data. */
function Crosshair() {
  return (
    <div aria-hidden className="pointer-events-none absolute inset-0">
      <div
        className="absolute left-0 right-0 top-1/2 h-px"
        style={{ background: 'oklch(1 0 0 / 4.5%)' }}
      />
      <div
        className="absolute bottom-0 top-0 left-1/2 w-px"
        style={{ background: 'oklch(1 0 0 / 4.5%)' }}
      />
    </div>
  )
}

/**
 * FLORA-style instrument nameplate framing a cell; sweeps stamp the varied
 * value, mock demo scenarios stamp their token in warn.
 */
function Nameplate({
  index = 0,
  right,
  warn = false,
}: {
  index?: number
  right?: string
  warn?: boolean
}) {
  return (
    <div className="mb-1.5 flex items-baseline justify-between">
      <span className="microlabel">cell {String(index + 1).padStart(2, '0')}</span>
      <span
        className={`microlabel ${warn ? 'text-warn' : right ? 'text-accent' : ''}`}
      >
        {right ?? 'flux.1-dev'}
      </span>
    </div>
  )
}

/** `DEMO SCENARIO: COLD` stamp text for a submitted demo-token prompt. */
function demoStamp(prompt: string | undefined): string | undefined {
  const token = prompt ? demoScenario(prompt) : null
  return token ? `demo scenario: ${token}` : undefined
}

function SeedPlate({ seed, offset = 0 }: { seed: number | null; offset?: number }) {
  if (seed === null) return null
  return (
    <div className="mt-1.5 flex">
      <span
        className="microlabel"
        title={offset > 0 ? `locked seed +${offset} — deterministic, not random` : undefined}
      >
        seed {seed}
        {offset > 0 && <span className="text-ink-label"> (+{offset})</span>}
      </span>
    </div>
  )
}

/** Krea/Sora bottom bar, instrument-styled: hairline surface, mono textarea, attached Generate. */
function PromptBar({
  value,
  onChange,
  onGenerate,
  busy,
  canGenerate,
  disabled = false,
  promptRef,
  params,
}: {
  value: string
  onChange: (next: string) => void
  onGenerate: () => void
  busy: boolean
  canGenerate: boolean
  /** pre-key: visible but inert, so the affordance is discoverable */
  disabled?: boolean
  promptRef: React.RefObject<HTMLTextAreaElement | null>
  params: FormParams
}) {
  return (
    <div
      className={`prompt-bar raised-lit flex items-stretch rounded-[6px] border border-hairline bg-raised ${disabled ? 'opacity-60' : ''}`}
    >
      <textarea
        ref={promptRef}
        aria-label="Prompt"
        rows={1}
        maxLength={2000}
        disabled={disabled}
        title={disabled ? 'set an API key to generate' : undefined}
        placeholder="a red fox in falling snow, cinematic lighting"
        className="max-h-[calc(3lh+1.6rem)] min-h-[calc(1lh+1.6rem)] flex-1 resize-none bg-transparent px-4 py-3 font-mono text-[13px] leading-relaxed text-ink [field-sizing:content] placeholder:text-ink-label"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            if (canGenerate) onGenerate()
          }
        }}
      />
      <ParamChips params={params} />
      <m.button
        type="button"
        disabled={!canGenerate}
        onClick={onGenerate}
        title={disabled ? 'set an API key to generate' : 'Enter or Cmd/Ctrl+Enter'}
        whileTap={{ scale: 0.98 }}
        transition={{ type: 'spring', stiffness: 500, damping: 30 }}
        className="btn-primary press-spring mr-1.5 h-9 shrink-0 self-center px-5 text-[13px] font-semibold"
      >
        {busy
          ? 'generating'
          : params.mode === 'sweep'
            ? 'Generate sweep ×4'
            : generateLabel(params.batch)}
      </m.button>
    </div>
  )
}

function focusRail(id: string) {
  const el = document.getElementById(id)
  if (!el) return
  if (
    el instanceof HTMLInputElement ||
    el instanceof HTMLSelectElement ||
    el instanceof HTMLButtonElement
  ) {
    el.focus()
    return
  }
  el.querySelector<HTMLElement>('button, input, select')?.focus()
}

/** Read-only mirrors of the rail params; clicking one focuses its input. */
function ParamChips({ params }: { params: FormParams }) {
  // in sweep mode the swept param's chip shows its range, not a stale constant
  const swept = params.mode === 'sweep' ? params.sweep : null
  const sweepTitle = swept
    ? `4 cells varying ${swept}: ${SWEEP_VALUES[swept].join(' / ')}`
    : ''
  // one chip grammar app-wide: lowercase mono `label value`
  const chips: { label: string; text: string; target: string; title: string }[] = [
    swept === 'size'
      ? { label: 'sweep', text: '512→1280', target: 'rail-sweep', title: sweepTitle }
      : {
          label: 'size',
          text: `${params.width}×${params.height}`,
          target: 'rail-size',
          title: 'Size — edit in the params rail',
        },
    swept === 'steps'
      ? { label: 'sweep', text: '4→28 steps', target: 'rail-sweep', title: sweepTitle }
      : {
          label: 'steps',
          text: String(params.steps),
          target: 'rail-steps',
          title: 'Inference steps',
        },
    swept === 'guidance'
      ? { label: 'sweep', text: 'g 1→10', target: 'rail-sweep', title: sweepTitle }
      : {
          label: 'guidance',
          text: params.guidance.toFixed(1),
          target: 'rail-guidance',
          title: 'Guidance scale',
        },
    {
      label: 'seed',
      text: params.seedMode === 'lock' && params.seed !== null ? String(params.seed) : 'dice',
      target: params.seedMode === 'lock' ? 'rail-seed' : 'rail-seed-mode',
      title: params.seedMode === 'lock' ? 'Locked seed' : 'New random seed each run',
    },
    ...(params.mode === 'batch' && params.batch > 1
      ? [
          {
            label: 'batch',
            text: `×${params.batch}`,
            target: 'rail-batch',
            title: `Batch of ${params.batch} — parallel workers`,
          },
        ]
      : []),
    {
      label: 'format',
      text: params.format,
      target: 'rail-format',
      title: 'Output format',
    },
  ]
  return (
    <div className="hidden shrink-0 items-center gap-1 self-center px-2 md:flex">
      {chips.map((chip) => (
        <button
          key={chip.target}
          type="button"
          title={chip.title}
          onClick={() => focusRail(chip.target)}
          className={`cursor-pointer whitespace-nowrap rounded border border-hairline-strong bg-raised px-1.5 py-0.5 font-mono text-[11px] text-ink-dim hover:border-[oklch(1_0_0/24%)] hover:text-ink`}
        >
          <span className="text-ink-label">{chip.label} </span>
          {chip.text}
        </button>
      ))}
    </div>
  )
}

/** Cell width for a min(`vh`vh, canvas width) cell aspect-matched to W×H. */
function cellWidth(width: number, height: number, vh: number): React.CSSProperties {
  const ar = width / height
  return { width: `min(${(vh * ar).toFixed(2)}vh, 100%)` }
}

/** Aspect the `.batch-cluster` sizing rules read; width lives in index.css. */
function clusterAr(width: number, height: number): React.CSSProperties {
  return { '--ar': width / height } as React.CSSProperties
}

/** Real prompts from the ledger; one click fills the bar. */
const EXAMPLE_PROMPTS = [
  'a red fox in falling snow, cinematic lighting',
  'lighthouse on a basalt cliff, long exposure',
  'great horned owl, studio portrait, rim light',
  'brutalist library interior, fog, volumetric light',
]

function ExamplePrompts({
  onPrompt,
  className = '',
}: {
  onPrompt: (prompt: string) => void
  className?: string
}) {
  return (
    <div className={`mt-4 flex flex-wrap justify-center gap-2 ${className}`}>
      {EXAMPLE_PROMPTS.map((prompt) => (
        <button
          key={prompt}
          type="button"
          onClick={() => onPrompt(prompt)}
          title="Fill the prompt bar"
          className="btn-ghost px-2.5 py-1 text-[11px]"
        >
          {prompt}
        </button>
      ))}
    </div>
  )
}

function EmptyCell({
  width,
  height,
  lockedSeed,
  onPrompt,
  onRunExperiment,
}: {
  width: number
  height: number
  lockedSeed: number | null
  onPrompt: (prompt: string) => void
  onRunExperiment: () => void
}) {
  return (
    <div className="flex w-full max-w-[900px] flex-col items-center">
    <div
      style={{ '--ar': width / height } as React.CSSProperties}
      className="empty-cell flex flex-col"
    >
      <Nameplate />
      <div
        style={{
          aspectRatio: `${width} / ${height}`,
          backgroundImage:
            'radial-gradient(closest-side, oklch(1 0 0 / 2.5%), transparent)',
        }}
        className="flex w-full items-center justify-center border border-dashed border-hairline-strong"
      >
        <span className="font-mono text-xs text-ink-label">
          {width} × {height} px
        </span>
      </div>
      <SeedPlate seed={lockedSeed} />
      <div className="mt-4 flex flex-col items-center gap-2 border border-hairline bg-surface/60 px-4 py-3 text-center">
        <p className="max-w-md font-mono text-[11px] leading-relaxed text-ink-dim">
          This is an instrument, not a gallery. Every number on screen is
          measured on the live endpoint — never estimated.
        </p>
        <button
          type="button"
          onClick={onRunExperiment}
          title="steps sweep 4/12/20/28, seed locked — watch wall time scale linearly"
          className="btn-ghost primary px-3 py-1.5 text-[11px]"
        >
          run the steps experiment
        </button>
      </div>
      </div>
      {/* wider than the cell so the four prompts sit on one wrapping row; below
          sm the cell + card already fill the canvas and the CTA must stay clear
          of the prompt bar */}
      <ExamplePrompts
        onPrompt={onPrompt}
        className="hidden w-full sm:flex"
      />
    </div>
  )
}

/** Hairline batch/sweep cells before submit: the grid the jobs will fill. */
function EmptyBatch({
  params,
  sweep,
  lockedSeed,
  onPrompt,
}: {
  params: FormParams
  sweep: SweepParam | null
  lockedSeed: number | null
  onPrompt: (prompt: string) => void
}) {
  const n = sweep ? 4 : params.batch
  return (
    <div
      style={clusterAr(params.width, params.height)}
      className={`batch-cluster solo flex flex-col ${n === 2 ? 'pair' : ''}`}
    >
      <div className="batch-grid grid grid-cols-2 gap-3">
        {Array.from({ length: n }, (_, i) => {
          const w = sweep === 'size' ? SWEEP_VALUES.size[i] : params.width
          const h = sweep === 'size' ? SWEEP_VALUES.size[i] : params.height
          return (
            <div key={i} className="flex min-w-0 flex-col">
              <Nameplate
                index={i}
                right={sweep ? sweepValueLabel(sweep, i) : undefined}
              />
              <div
                style={{
                  aspectRatio: `${w} / ${h}`,
                  backgroundImage:
                    'radial-gradient(closest-side, oklch(1 0 0 / 2.5%), transparent)',
                }}
                className="flex w-full items-center justify-center border border-dashed border-hairline-strong"
              >
                <span className="font-mono text-xs text-ink-label">
                  {w} × {h} px
                </span>
              </div>
              {/* sweep: the seed is constant — it lives once in the rail */}
              {!sweep && lockedSeed !== null && (
                <SeedPlate seed={(lockedSeed + i) % (SEED_MAX + 1)} offset={i} />
              )}
            </div>
          )
        })}
      </div>
      {sweep && (
        <p className="mt-2 text-center font-mono text-[11px] text-ink-dim">
          one variable — {sweep} — four measured cells, seed{' '}
          {lockedSeed ?? '—'} held constant
        </p>
      )}
      <ExamplePrompts onPrompt={onPrompt} />
    </div>
  )
}

function GeneratingCell({
  run,
  params,
  lockedSeed,
  onCancel,
}: {
  run: JobRun
  params: FormParams
  lockedSeed: number | null
  onCancel: () => void
}) {
  const progress = run.job?.progress ?? null

  // last two frames stay mounted: the newer one fades in over its predecessor
  const framesRef = useRef<PreviewFrame[]>([])
  const preview = run.preview
  if (preview && framesRef.current[framesRef.current.length - 1]?.seq !== preview.seq) {
    framesRef.current = [...framesRef.current.slice(-1), preview]
  }
  const frames = preview ? framesRef.current : []

  const demo = demoStamp(run.request?.prompt ?? params.prompt)
  return (
    <div style={cellWidth(params.width, params.height, 50)} className="flex flex-col">
      <Nameplate right={demo} warn={demo !== undefined} />
      <RunningFrame
        run={run}
        frames={frames}
        progress={progress}
        width={params.width}
        height={params.height}
        numPx={30}
      />
      <StageTimeline
        stages={run.stages}
        progress={progress}
        elapsedMs={run.elapsedMs}
        prompt={params.prompt}
        onCancel={onCancel}
      />
      <SeedPlate seed={lockedSeed} />
    </div>
  )
}

/** The live cell body: preview frames crossfading under a percent readout. */
function RunningFrame({
  run,
  frames,
  progress,
  width,
  height,
  numPx,
}: {
  run: JobRun
  frames: PreviewFrame[]
  progress: { percent: number; step: number; total: number } | null
  width: number
  height: number
  numPx: number
}) {
  const activeStage = run.stages.find((s) => s.endedAtMs === null)
  return (
    <div
      style={{ aspectRatio: `${width} / ${height}` }}
      className="relative flex w-full flex-col items-center justify-center gap-1 overflow-hidden border border-hairline bg-surface/40"
    >
      {frames.map((f, i) => (
        <m.img
          key={f.seq}
          src={f.src}
          alt={i === frames.length - 1 ? `Denoising preview, step ${f.step} of ${f.total}` : ''}
          aria-hidden={i < frames.length - 1 || undefined}
          data-testid="preview-frame"
          initial={{ opacity: i === 0 ? 1 : 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.3 }}
          className="absolute inset-0 h-full w-full object-cover"
        />
      ))}
      {frames.length > 0 && progress ? (
        <div
          role="progressbar"
          aria-label="Denoising progress"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress.percent}
          style={{ background: 'oklch(0.17 0.005 250 / 92%)' }}
          className="absolute bottom-0 left-0 flex items-baseline gap-2.5 py-1.5 pl-3 pr-4"
        >
          <span className="display-number text-ink" style={{ fontSize: numPx }} data-testid="dyn">
            {progress.percent}%
          </span>
          <span className="microlabel whitespace-nowrap font-mono text-ink-dim" data-testid="dyn">
            {numPx === 30 ? 'preview · step' : 'step'} {progress.step}/{progress.total}
          </span>
        </div>
      ) : (
        <span
          role={progress ? 'progressbar' : undefined}
          aria-label={progress ? 'Denoising progress' : undefined}
          aria-valuemin={progress ? 0 : undefined}
          aria-valuemax={progress ? 100 : undefined}
          aria-valuenow={progress?.percent}
          className="flex flex-col items-center"
        >
          <span className="display-number text-ink" style={{ fontSize: numPx }} data-testid="dyn">
            {progress ? `${progress.percent}%` : fmtS(run.elapsedMs)}
          </span>
          <span className="microlabel mt-1" data-testid="dyn">
            {progress ? `step ${progress.step}/${progress.total}` : (activeStage?.label ?? 'submitting')}
          </span>
        </span>
      )}
    </div>
  )
}

/** `4→3.1s · 12→9.1s · …` once every sweep cell is terminal; null before. */
function sweepSummaryText(sweep: SweepParam | null, runs: JobRun[]): string | null {
  if (!sweep || runs.length === 0 || !runs.every((r) => r.phase === 'done')) {
    return null
  }
  return SWEEP_VALUES[sweep]
    .map((v, i) => {
      const wall = runs[i]?.wallMs
      return `${v}→${wall != null ? `${(wall / 1000).toFixed(1)}s` : '—'}`
    })
    .join(' · ')
}

function BatchCanvas({
  runs,
  params,
  sweep,
  lockedSeed,
  batchWallMs,
  onRetryCell,
  onEditPrompt,
  onCancelAll,
  onAdoptSeed,
}: {
  runs: JobRun[]
  params: FormParams
  sweep: SweepParam | null
  lockedSeed: number | null
  batchWallMs: number | null
  onRetryCell: (index: number) => void
  onEditPrompt: () => void
  onCancelAll: () => void
  onAdoptSeed: (seed: number) => void
}) {
  const n = runs.length
  return (
    <div
      style={clusterAr(params.width, params.height)}
      className={`batch-cluster flex flex-col gap-3 xl:flex-row xl:items-start xl:gap-4 ${n === 2 ? 'pair' : ''}`}
    >
      <div className="batch-grid grid shrink-0 grid-cols-2 gap-3">
        {runs.map((run, i) => (
          <BatchCell
            key={i}
            run={run}
            index={i}
            n={n}
            params={params}
            sweep={sweep}
            lockedSeed={lockedSeed}
            onRetry={() => onRetryCell(i)}
            onEditPrompt={onEditPrompt}
            onAdoptSeed={onAdoptSeed}
          />
        ))}
      </div>
      <div className="min-w-0 xl:flex-1">
        <BatchTimeline
          runs={runs}
          prompt={params.prompt}
          batchWallMs={batchWallMs}
          kind={sweep ? 'sweep' : 'batch'}
          sweepSummary={sweepSummaryText(sweep, runs)}
          onCancelAll={onCancelAll}
        />
      </div>
    </div>
  )
}

function BatchCell({
  run,
  index,
  n,
  params,
  sweep,
  lockedSeed,
  onRetry,
  onEditPrompt,
  onAdoptSeed,
}: {
  run: JobRun
  index: number
  n: number
  params: FormParams
  sweep: SweepParam | null
  lockedSeed: number | null
  onRetry: () => void
  onEditPrompt: () => void
  onAdoptSeed: (seed: number) => void
}) {
  // display-number scale: 30px hero → ~20px at 2-up → ~16px at 2×2
  const numPx = n === 2 ? 20 : 16
  const envelope = toEnvelope(run)
  const completed =
    run.phase === 'done' && run.job?.status === 'COMPLETED' && run.job.result
  const seed = run.request?.seed ?? null
  // size sweeps vary per cell; the submitted request is the truth
  const cellW = run.request?.width ?? params.width
  const cellH = run.request?.height ?? params.height

  // per-cell preview crossfade + handoff into the final image
  const framesRef = useRef<PreviewFrame[]>([])
  const handoffRef = useRef<string | null>(null)
  const preview = run.preview
  if (preview) {
    handoffRef.current = preview.src
    if (framesRef.current[framesRef.current.length - 1]?.seq !== preview.seq) {
      framesRef.current = [...framesRef.current.slice(-1), preview]
    }
  } else if (run.phase === 'idle' || run.phase === 'submitting') {
    handoffRef.current = null
    framesRef.current = []
  }
  const frames = preview ? framesRef.current : []

  const demo = demoStamp(run.request?.prompt)
  return (
    <div className="flex min-w-0 flex-col">
      <Nameplate
        index={index}
        right={sweep ? sweepValueLabel(sweep, index) : demo}
        warn={!sweep && demo !== undefined}
      />
      {completed ? (
        <CellDone
          run={run}
          numPx={numPx}
          handoffSrc={handoffRef.current}
          sweptLabel={sweep ? sweepValueLabel(sweep, index) : null}
          onAdoptSeed={onAdoptSeed}
        />
      ) : envelope ? (
        <div
          style={{ aspectRatio: `${cellW} / ${cellH}` }}
          className="flex w-full items-center justify-center overflow-auto border border-hairline bg-surface/40"
        >
          <ErrorEnvelope
            error={envelope}
            compact={n === 4}
            retryLabel="retry cell"
            onRetry={onRetry}
            onEditPrompt={onEditPrompt}
          />
        </div>
      ) : (
        <RunningFrame
          run={run}
          frames={frames}
          progress={run.job?.progress ?? null}
          width={cellW}
          height={cellH}
          numPx={numPx}
        />
      )}
      {/* sweep: seed is a held constant, stated once in the rail */}
      {!sweep && lockedSeed !== null && <SeedPlate seed={seed} offset={index} />}
    </div>
  )
}

/** Copy / adopt split on a completed cell's seed: reuse it as the locked seed. */
function SeedActions({
  seed,
  textCls,
  onAdopt,
}: {
  seed: number
  textCls: string
  onAdopt: (seed: number) => void
}) {
  return (
    <span className="flex items-baseline gap-1" data-testid="cell-seed">
      <span className="microlabel">seed</span>
      <CopyValue value={String(seed)} className={`${textCls} text-ink`} />
      <button
        type="button"
        title={`lock seed ${seed} for the next run`}
        onClick={() => onAdopt(seed)}
        className="btn-ghost px-1.5 py-0 text-[11px]"
      >
        adopt
      </button>
    </span>
  )
}

/** Completed batch cell: image plus its own compact metadata strip. */
function CellDone({
  run,
  numPx,
  handoffSrc,
  sweptLabel = null,
  onAdoptSeed,
}: {
  run: JobRun
  numPx: number
  handoffSrc: string | null
  /** sweep cells: the varied value becomes the strip's dominant label */
  sweptLabel?: string | null
  onAdoptSeed: (seed: number) => void
}) {
  const job = run.job!
  const result = job.result!
  const [imgSrc, setImgSrc] = useState<string | null>(null)
  const [lightbox, setLightbox] = useState(false)
  const [underlay, setUnderlay] = useState<string | null>(handoffSrc)
  useEffect(() => {
    if (!imgSrc || !underlay) return
    const timer = setTimeout(() => setUnderlay(null), 600)
    return () => clearTimeout(timer)
  }, [imgSrc, underlay])
  const filename = imageFilename(result.seed, result.width, result.height, result.format)
  return (
    <figure className="flex w-full flex-col">
      <button
        type="button"
        onClick={() => imgSrc && setLightbox(true)}
        disabled={!imgSrc}
        title="View at full size"
        aria-label="View image at full size"
        className="block w-full cursor-zoom-in disabled:cursor-default"
      >
        <JobImage
          jobId={job.job_id}
          thumbhash={result.thumbhash}
          imageBase64={result.image_base64}
          format={result.format}
          alt="Generated image"
          eager
          onSrc={setImgSrc}
          underlaySrc={underlay}
          className="w-full border border-hairline"
          style={{ aspectRatio: `${result.width} / ${result.height}` }}
        />
      </button>
      <figcaption className="panel flex flex-wrap items-baseline gap-x-3 gap-y-0.5 border border-t-0 border-hairline px-2.5 py-1.5 font-mono text-[11px] text-ink-dim">
        {sweptLabel ? (
          // the experiment readout: varied value · measured wall, nothing else
          <span className="flex items-baseline gap-1.5" data-testid="sweep-stamp">
            <span className="display-number text-ink" style={{ fontSize: numPx }}>
              {sweptLabel}
            </span>
            <span aria-hidden className="text-ink-faint">
              ·
            </span>
            {run.wallMs !== null && (
              <span
                className="display-number text-ink"
                style={{ fontSize: numPx }}
                data-testid="dyn"
              >
                {fmtS(run.wallMs)}
              </span>
            )}
          </span>
        ) : (
          <>
            {run.wallMs !== null && (
              <span className="flex items-baseline gap-1.5" data-testid="dyn">
                <span className="microlabel">wall</span>
                <span className="display-number text-ink" style={{ fontSize: numPx }}>
                  {fmtS(run.wallMs)}
                </span>
              </span>
            )}
            <span className="flex items-baseline gap-1" data-testid="dyn">
              <span className="microlabel">inf</span>
              <span className="text-ink">{result.inference_seconds.toFixed(1)} s</span>
            </span>
            <SeedActions seed={result.seed} textCls="text-[11px]" onAdopt={onAdoptSeed} />
          </>
        )}
        {imgSrc && (
          <button
            type="button"
            onClick={() => downloadUrl(imgSrc, filename)}
            title={`Save ${filename}`}
            className="btn-ghost ml-auto px-1.5 py-0.5 text-[11px]"
          >
            download {result.format}
          </button>
        )}
      </figcaption>
      {lightbox && imgSrc && (
        <Lightbox
          src={imgSrc}
          width={result.width}
          height={result.height}
          seed={result.seed}
          onClose={() => setLightbox(false)}
        />
      )}
    </figure>
  )
}

function CompletedCell({
  run,
  handoffSrc = null,
  onAdoptSeed,
}: {
  run: JobRun
  handoffSrc?: string | null
  onAdoptSeed: (seed: number) => void
}) {
  const job = run.job!
  const result = job.result!
  const demo = demoStamp(run.request?.prompt)
  // significant queue time stays on the record: the caption keeps the split
  const queuedMs = queuedMsOf(run)
  const inferMs = inferenceMsOf(run)
  const coldSplit = queuedMs > COLD_QUEUE_MS && run.wallMs !== null
  const [imgSrc, setImgSrc] = useState<string | null>(null)
  const [lightbox, setLightbox] = useState(false)
  // preview underlay lives only through the crossfade, then drops from memory
  const [underlay, setUnderlay] = useState<string | null>(handoffSrc)
  useEffect(() => {
    if (!imgSrc || !underlay) return
    const timer = setTimeout(() => setUnderlay(null), 600)
    return () => clearTimeout(timer)
  }, [imgSrc, underlay])
  const filename = imageFilename(result.seed, result.width, result.height, result.format)
  return (
    <figure style={cellWidth(result.width, result.height, 72)} className="flex flex-col">
      <Nameplate right={demo} warn={demo !== undefined} />
      <button
        type="button"
        onClick={() => imgSrc && setLightbox(true)}
        disabled={!imgSrc}
        title="View at full size"
        aria-label="View image at full size"
        className="block w-full cursor-zoom-in disabled:cursor-default"
      >
        <JobImage
          jobId={job.job_id}
          thumbhash={result.thumbhash}
          imageBase64={result.image_base64}
          format={result.format}
          alt="Generated image"
          eager
          onSrc={setImgSrc}
          underlaySrc={underlay}
          className="w-full border border-hairline"
          style={{ aspectRatio: `${result.width} / ${result.height}` }}
        />
      </button>
      <figcaption className="panel flex flex-wrap items-baseline gap-x-5 gap-y-1 border border-t-0 border-hairline px-4 py-2.5 font-mono text-xs text-ink-dim">
        {run.wallMs !== null ? (
          <span className="flex items-baseline gap-2" data-testid="dyn">
            <span className="microlabel">wall</span>
            <span className="display-number text-ink">{fmtS(run.wallMs)}</span>
            <span className="text-ink-label">bench p50 {BENCH_WARM_EXEC_P50_S} s</span>
          </span>
        ) : (
          <span data-testid="dyn">replayed · wall not observed</span>
        )}
        {coldSplit && (
          <span className="flex items-baseline gap-1.5 text-warn" data-testid="cold-split">
            <span
              title={COLD_TOOLTIP}
              className="cursor-help font-mono text-[11px]"
            >
              queued {fmtS(queuedMs)} (likely cold)
              {inferMs !== null && ` · inference ${fmtS(inferMs)}`}
            </span>
          </span>
        )}
        <span className="flex items-baseline gap-1.5" data-testid="dyn">
          <span className="microlabel">inference</span>
          <span className="text-[13px] text-ink">{result.inference_seconds.toFixed(1)} s</span>
        </span>
        <SeedActions seed={result.seed} textCls="text-[13px]" onAdopt={onAdoptSeed} />
        {imgSrc && (
          <button
            type="button"
            onClick={() => downloadUrl(imgSrc, filename)}
            title={`Save ${filename}`}
            className="btn-ghost ml-auto px-2.5 py-1 text-[11px]"
          >
            download {result.format}
          </button>
        )}
      </figcaption>
      {lightbox && imgSrc && (
        <Lightbox
          src={imgSrc}
          width={result.width}
          height={result.height}
          seed={result.seed}
          onClose={() => setLightbox(false)}
        />
      )}
    </figure>
  )
}

function toEnvelope(run: JobRun): EnvelopeData | null {
  if (run.error) {
    return {
      code: run.error.code,
      message: run.error.message,
      suggestion: run.error.suggestion,
      correlationId: run.error.correlationId,
      retryDeadline: run.error.retryDeadline,
    }
  }
  const job = run.job
  if (
    run.phase === 'done' &&
    job &&
    job.status !== 'COMPLETED' &&
    job.status !== 'QUEUED' &&
    job.status !== 'IN_PROGRESS'
  ) {
    const err = job.error
    return {
      code: err?.code ?? job.status,
      message: err?.message || `Job ended ${job.status}.`,
      suggestion: err?.suggestion ?? null,
      correlationId: err?.correlation_id ?? null,
      retryDeadline: null,
    }
  }
  return null
}
