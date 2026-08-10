import { useCallback, useEffect, useState } from 'react'
import { ApiError, fetchMetrics } from '../api/client'
import type { JobStatus, LatencyPercentiles, MetricsView } from '../api/types'
import { BENCH_WARM_EXEC_P50_S } from '../lib/bench'
import { navigate } from '../lib/router'
import { useAppState } from '../lib/store'
import { ErrorEnvelope, type EnvelopeData } from '../components/ErrorEnvelope'
import { NoKeyState } from '../components/KeyPopover'
import { ThroughputBars } from '../components/ThroughputBars'

const POLL_MS = 10_000

const STATUS_ORDER: JobStatus[] = [
  'QUEUED',
  'IN_PROGRESS',
  'COMPLETED',
  'FAILED',
  'TIMED_OUT',
  'CANCELLED',
  'BLOCKED',
]

/** Same treatment the ledger gives these codes: errors are chipped, everything else is plain. */
function chipTone(status: string): string | null {
  if (status === 'FAILED' || status === 'TIMED_OUT') return ''
  if (status === 'BLOCKED') return 'warn'
  return null
}

function fmtSeconds(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${value.toFixed(1)} s`
}

/** A configured threshold, not a measurement: 30, not 30.0. */
function fmtThreshold(value: number): string {
  return `${Number.isInteger(value) ? value : value.toFixed(1)} s`
}

function fmtUsd(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `$${value < 1 ? value.toFixed(4) : value.toFixed(2)}`
}

function fmtCount(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : String(value)
}

/**
 * Milliseconds the window covers: newest completion minus oldest. Null when the
 * window is empty; 0 when it holds a single job — both are non-spans, and
 * neither may be turned into a rate.
 */
function windowSpanMs(metrics: MetricsView): number | null {
  const { window_started_at, window_ended_at } = metrics
  if (window_started_at === null || window_ended_at === null) return null
  return Math.max(
    0,
    new Date(window_ended_at).getTime() - new Date(window_started_at).getTime(),
  )
}

/**
 * What the window actually was. It is "the newest N completions", not a fixed
 * period, so the count alone says nothing about the stretch of time it covers.
 */
function windowNote(metrics: MetricsView) {
  const n = metrics.completed_in_window
  const spanMs = windowSpanMs(metrics)
  const tail =
    spanMs === null ? 'no window yet'
    : spanMs === 0 ? 'one instant'
    : `${fmtSpan(spanMs)} span`
  return (
    <span data-testid="window-span">
      {`newest ${n} completed · ${tail}`}
    </span>
  )
}

/** Coarse duration, one unit: the span is context, not a measurement. */
function fmtSpan(ms: number): string {
  const s = ms / 1000
  if (s < 90) return `${Math.round(s)} s`
  const min = s / 60
  if (min < 90) return `${Math.round(min)} min`
  const h = min / 60
  if (h < 48) return `${h.toFixed(1)} h`
  return `${(h / 24).toFixed(1)} d`
}

export function OperateView({ active }: { active: boolean }) {
  const { key } = useAppState()
  const [metrics, setMetrics] = useState<MetricsView | null>(null)
  const [error, setError] = useState<ApiError | Error | null>(null)
  const [now, setNow] = useState(() => Date.now())

  // one clock for the whole view: generated_at age ticks without re-polling
  useEffect(() => {
    if (!active) return
    const tick = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(tick)
  }, [active])

  const load = useCallback(() => {
    return fetchMetrics()
      .then((next) => {
        setMetrics(next)
        setError(null)
      })
      .catch((err: unknown) => {
        setError(err instanceof Error ? err : new Error('Network failure.'))
      })
  }, [])

  // polls only while the view is on screen AND the tab is foregrounded; a
  // backgrounded tab must not bill GPU-adjacent endpoints for nothing
  useEffect(() => {
    if (!active || !key) return
    let timer: number | undefined
    const start = () => {
      void load()
      timer = window.setInterval(() => void load(), POLL_MS)
    }
    const stop = () => {
      if (timer !== undefined) clearInterval(timer)
      timer = undefined
    }
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        if (timer === undefined) start()
      } else stop()
    }
    if (document.visibilityState === 'visible') start()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [active, key, load])

  if (!key) return <NoKeyState />

  const unauthorized = error instanceof ApiError && error.status === 401
  if (unauthorized) return <NoKeyState />

  if (error && !metrics) {
    const envelope: EnvelopeData =
      error instanceof ApiError
        ? {
            code: error.code,
            message: error.message,
            suggestion: error.suggestion,
            correlationId: error.correlationId,
            retryDeadline: error.retryDeadline,
          }
        : {
            code: 'NETWORK_FAILURE',
            message: 'The metrics request did not reach the gateway.',
            suggestion: 'Check the gateway is reachable, then retry.',
            correlationId: null,
            retryDeadline: null,
          }
    return (
      <div className="flex h-full items-center justify-center p-6">
        <ErrorEnvelope
          error={envelope}
          onRetry={() => void load()}
          onEditPrompt={() => void load()}
          retryLabel="retry"
        />
      </div>
    )
  }

  return (
    <div className="mx-auto flex h-full max-w-[1440px] flex-col overflow-auto px-6 py-4">
      <Header metrics={metrics} now={now} stale={error !== null} />
      {metrics !== null && (
        <>
          {metrics.completed_in_window === 0 && (
            <p className="mb-3 font-mono text-xs text-ink-label">
              no completed jobs in the window yet — every panel below reads its
              honest zero, and fills as jobs run
            </p>
          )}
          {/* the panel grid is the instrument face: it fills the frame, and its
              hairlines run edge to edge rather than floating as cards */}
          <div className="grid flex-1 grid-cols-1 border border-hairline lg:grid-cols-12 lg:grid-rows-[auto_minmax(0,1fr)_auto]">
            <Endpoint metrics={metrics} />
            <Latency metrics={metrics} />
            <Throughput metrics={metrics} />
            <Jobs metrics={metrics} />
            <Cost metrics={metrics} />
          </div>
        </>
      )}
    </div>
  )
}

function Header({
  metrics,
  now,
  stale,
}: {
  metrics: MetricsView | null
  now: number
  stale: boolean
}) {
  const ageS =
    metrics === null
      ? null
      : Math.max(0, Math.round((now - new Date(metrics.generated_at).getTime()) / 1000))
  return (
    <div className="mb-4 flex flex-wrap items-baseline gap-x-4 gap-y-1">
      <span className="microlabel">endpoint telemetry</span>
      <span className="font-mono text-xs text-ink-label" data-testid="dyn">
        {ageS === null
          ? 'reading…'
          : `snapshot ${ageS} s old · repolled every ${POLL_MS / 1000} s while this tab is visible`}
      </span>
      {stale && metrics !== null && (
        <span className="code-chip warn">POLL FAILING · LAST GOOD READING</span>
      )}
    </div>
  )
}

function Panel({
  label,
  note,
  span,
  className = '',
  children,
}: {
  label: string
  note?: React.ReactNode
  /** literal Tailwind class: the grid is one column until lg */
  span: string
  className?: string
  children: React.ReactNode
}) {
  return (
    <section
      aria-label={label}
      className={`flex flex-col gap-3 p-4 ${span} ${className}`}
    >
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="microlabel">{label}</h2>
        {note && (
          <span className="font-mono text-[11px] text-ink-label">{note}</span>
        )}
      </div>
      {children}
    </section>
  )
}

/** One instrument readout: micro-label over a big mono value. */
function Readout({
  label,
  value,
  unit,
  dim = false,
  title,
  className = '',
}: {
  label: string
  value: string
  unit?: string
  dim?: boolean
  title?: string
  className?: string
}) {
  return (
    <div className={`flex flex-col gap-0.5 ${className}`} title={title}>
      <span className="microlabel">{label}</span>
      <span
        className={`display-number ${dim || value === '—' ? 'text-ink-faint' : 'text-ink'}`}
      >
        {value}
        {unit && (
          <span className="ml-1 font-mono text-xs font-normal text-ink-label">
            {unit}
          </span>
        )}
      </span>
    </div>
  )
}

/** Column rule between readouts in a strip; the first column carries none. */
const RULE = 'lg:border-l lg:border-hairline lg:pl-4 lg:first:border-l-0 lg:first:pl-0'

function Endpoint({ metrics }: { metrics: MetricsView }) {
  const { queue, reconciler } = metrics.upstream
  // `status` says what kind of reading it is; `stale` says whether to trust the
  // numbers. Both are the gateway's judgement — this view holds no cutoff of its own.
  const unknown = queue.status !== 'ok'
  const stale = queue.stale

  return (
    <Panel
      label="endpoint"
      span="lg:col-span-12"
      note="upstream queue · shared topology, not per-key"
      className="border-b border-hairline"
    >
      {/* column rules, not gaps: six readouts read as one instrument strip */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
        <Readout
          label="in queue"
          value={fmtCount(queue.in_queue)}
          dim={stale}
          className={RULE}
        />
        <Readout
          label="in progress"
          value={fmtCount(queue.in_progress)}
          dim={stale}
          className={RULE}
        />
        <Readout
          label="workers run"
          value={fmtCount(queue.workers_running)}
          dim={stale}
          className={RULE}
        />
        <Readout
          label="workers idle"
          value={fmtCount(queue.workers_idle)}
          dim={stale}
          className={RULE}
        />
        <Readout
          label="active now"
          value={String(metrics.jobs.active_now)}
          title="this key's non-terminal jobs"
          className={RULE}
        />
        <Readout
          label="last hour"
          value={String(metrics.jobs.last_hour)}
          unit="jobs"
          title="this key's jobs created in the trailing hour"
          className={RULE}
        />
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 font-mono text-[11px] text-ink-label">
        {unknown ? (
          <span data-testid="queue-staleness" className="flex items-center gap-2">
            <span className="code-chip warn">STALE</span>
            <span className="text-warn">
              queue reading unavailable — the gateway holds no cached reading yet,
              so the four readouts above are unknown, not zero
            </span>
          </span>
        ) : stale ? (
          <span data-testid="queue-staleness" className="flex items-center gap-2">
            <span className="code-chip warn">STALE</span>
            <span className="text-warn">
              queue reading {fmtSeconds(queue.age_s)} old · the gateway calls it
              stale past {fmtThreshold(queue.stale_after_s)} — the readouts
              above are the last cached poll, not live
            </span>
          </span>
        ) : (
          <span data-testid="queue-staleness">
            queue reading {fmtSeconds(queue.age_s)} old · stale past{' '}
            {fmtThreshold(queue.stale_after_s)}
          </span>
        )}
        <span
          data-testid="reconciler-liveness"
          className={reconciler.stale ? 'text-warn' : undefined}
        >
          reconciler {reconciler.status}
          {reconciler.last_tick_s !== null
            ? ` · last tick ${fmtSeconds(reconciler.last_tick_s)} ago`
            : ' · no tick observed'}
          {` · stale past ${fmtThreshold(reconciler.stale_after_s)}`}
        </span>
      </div>
    </Panel>
  )
}

function LatencyRow({
  label,
  stats,
}: {
  label: string
  stats: LatencyPercentiles
}) {
  return (
    <tr className="border-b border-hairline align-baseline">
      <td className="microlabel py-1.5 pr-3">{label}</td>
      {[stats.p50_s, stats.p95_s, stats.max_s].map((value, i) => (
        <td
          key={i}
          className="py-1.5 pl-3 text-right font-mono text-[13px] whitespace-nowrap text-ink"
        >
          {fmtSeconds(value)}
        </td>
      ))}
    </tr>
  )
}

/** Bar-track geometry: label 86px + gap 8 + track + gap 8 + value 52px. */
const MARKER_LEFT = (pct: number) =>
  `calc(94px + (100% - 154px) * ${(pct / 100).toFixed(4)})`

/** Horizontal bar against a shared scale, with the committed p50 as a marker. */
function LatencyBar({
  label,
  value,
  scaleS,
  measured,
}: {
  label: string
  value: number | null
  scaleS: number
  measured: boolean
}) {
  const pct = value === null ? 0 : Math.min(100, (value / scaleS) * 100)
  return (
    <div className="flex items-center gap-2">
      <span className="w-[86px] shrink-0 font-mono text-[11px] text-ink-label">
        {label}
      </span>
      <span className="relative h-2.5 min-w-0 flex-1 bg-[oklch(1_0_0/6%)]">
        <span
          style={{ width: `${pct}%` }}
          className={`absolute inset-y-0 left-0 ${
            measured ? 'bg-accent' : 'bg-[oklch(1_0_0/28%)]'
          }`}
        />
      </span>
      <span className="w-[52px] shrink-0 text-right font-mono text-[11px] text-ink-dim">
        {fmtSeconds(value)}
      </span>
    </div>
  )
}

function Latency({ metrics }: { metrics: MetricsView }) {
  const { inference, wall } = metrics.latency
  const observed = [
    inference.p50_s,
    inference.p95_s,
    wall.p50_s,
    wall.p95_s,
    inference.max_s,
    wall.max_s,
  ].filter((v): v is number => v !== null && v !== undefined)
  const scaleS = Math.max(BENCH_WARM_EXEC_P50_S, ...observed) * 1.08
  const markerPct = (BENCH_WARM_EXEC_P50_S / scaleS) * 100
  const delta =
    inference.p50_s === null || inference.p50_s === undefined
      ? null
      : inference.p50_s - BENCH_WARM_EXEC_P50_S

  return (
    <Panel
      label="latency"
      span="lg:col-span-5"
      note={windowNote(metrics)}
      className="border-b border-hairline lg:border-r"
    >
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-hairline">
            <th />
            {['p50', 'p95', 'max'].map((head) => (
              <th
                key={head}
                className="microlabel py-1 pl-3 text-right font-normal"
              >
                {head}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <LatencyRow label="inference" stats={inference} />
          <LatencyRow label="wall" stats={wall} />
        </tbody>
      </table>

      <div className="relative flex flex-1 flex-col justify-evenly gap-1.5 pt-5">
        {/* the committed baseline, same idiom as the session sparkline's p50 line */}
        <span
          aria-hidden
          style={{ left: MARKER_LEFT(markerPct) }}
          className="absolute inset-y-0 top-5 w-px bg-[oklch(1_0_0/38%)]"
        />
        <span
          aria-hidden
          style={{ left: MARKER_LEFT(markerPct) }}
          className="absolute top-0 -translate-x-1/2 font-mono text-[10px] whitespace-nowrap text-ink-label"
        >
          {`bench ${BENCH_WARM_EXEC_P50_S}s`}
        </span>
        <LatencyBar
          label="inf p50"
          value={inference.p50_s}
          scaleS={scaleS}
          measured
        />
        <LatencyBar
          label="inf p95"
          value={inference.p95_s}
          scaleS={scaleS}
          measured
        />
        <LatencyBar
          label="wall p50"
          value={wall.p50_s}
          scaleS={scaleS}
          measured={false}
        />
        <LatencyBar
          label="wall p95"
          value={wall.p95_s}
          scaleS={scaleS}
          measured={false}
        />
      </div>
      <p
        title="BENCHMARKS.md steps sweep: 28 steps at 1024², N=10, exec p50"
        className="cursor-help font-mono text-[11px] text-ink-label"
      >
        {`marker = committed bench p50 ${BENCH_WARM_EXEC_P50_S} s`}
        {delta === null
          ? ' · nothing measured here yet'
          : ` · measured inference p50 runs ${delta >= 0 ? '+' : '−'}${Math.abs(delta).toFixed(1)} s against it`}
      </p>
    </Panel>
  )
}

function Throughput({ metrics }: { metrics: MetricsView }) {
  return (
    <Panel
      label="throughput"
      span="lg:col-span-7"
      note="completions per utc hour"
      className="border-b border-hairline"
    >
      <ThroughputBars buckets={metrics.throughput} />
    </Panel>
  )
}

function Jobs({ metrics }: { metrics: MetricsView }) {
  const counts = metrics.jobs.by_status
  return (
    <Panel
      label="jobs"
      span="lg:col-span-7"
      note="all-time for this key"
      className="border-b border-hairline lg:border-b-0 lg:border-r"
    >
      <div className="flex flex-wrap gap-x-6 gap-y-3">
        {STATUS_ORDER.map((status) => {
          const count = counts[status] ?? 0
          const tone = chipTone(status)
          const value =
            tone !== null && count > 0 ? (
              <span className={`code-chip ${tone} px-2 text-[20px] leading-[26px]`}>
                {count}
              </span>
            ) : (
              <span
                className={`font-mono text-[22px] leading-[28px] ${count === 0 ? 'text-ink-faint' : 'text-ink'}`}
              >
                {count}
              </span>
            )
          const body = (
            <>
              <span className="microlabel">{status.toLowerCase()}</span>
              <span className="flex h-7 items-center">{value}</span>
            </>
          )
          return count > 0 ? (
            <a
              key={status}
              href={`/gallery?status=${status}`}
              onClick={(e) => {
                e.preventDefault()
                navigate(`/gallery?status=${status}`)
              }}
              title={`Open the ledger filtered to ${status}`}
              className="flex flex-col items-start gap-0.5 hover:opacity-80"
            >
              {body}
            </a>
          ) : (
            <span key={status} className="flex flex-col items-start gap-0.5">
              {body}
            </span>
          )
        })}
      </div>
      <p className="font-mono text-[11px] text-ink-label">
        a non-zero count opens the ledger filtered to that status
      </p>
    </Panel>
  )
}

function Cost({ metrics }: { metrics: MetricsView }) {
  const {
    estimated_cost_usd,
    estimated_cost_usd_per_job,
    exec_seconds_in_window,
    gpu_rate_usd_hr,
  } = metrics.cost
  const spanMs = windowSpanMs(metrics)
  // a rate needs a stretch of time and more than one sample: one job, or a
  // window that collapsed to an instant, extrapolates to nothing honest
  const hours = spanMs === null || spanMs === 0 ? null : spanMs / 3_600_000
  const perHour =
    hours === null || metrics.completed_in_window < 2
      ? null
      : estimated_cost_usd / hours
  return (
    <Panel
      label="cost"
      span="lg:col-span-5"
      note="estimated, not billing data"
    >
      <div className="grid grid-cols-2 gap-4">
        <Readout
          label="est. per job"
          value={fmtUsd(estimated_cost_usd_per_job)}
          title="estimated GPU cost per completed job in the window"
        />
        <Readout
          label="est. in window"
          value={fmtUsd(estimated_cost_usd)}
          title="estimated GPU cost across the whole window"
        />
      </div>
      <div className="flex flex-col gap-1 font-mono text-[11px] leading-relaxed text-ink-label">
        {/* the identity the gateway publishes, spelled out: every term is on
            screen, so the estimate can be checked rather than trusted */}
        <p data-testid="cost-basis">
          {`basis: ${exec_seconds_in_window.toFixed(1)} exec-seconds × $${gpu_rate_usd_hr.toFixed(2)}/GPU-hr ÷ 3600 = ${fmtUsd(estimated_cost_usd)}`}
        </p>
        {perHour !== null && spanMs !== null && (
          <p data-testid="cost-rate" className="text-ink-dim">
            {`${fmtUsd(estimated_cost_usd)} over ${fmtSpan(spanMs)} ≈ ${fmtUsd(perHour)}/h`}
          </p>
        )}
        <p>
          {`over the newest ${metrics.window} completed jobs (${metrics.completed_in_window} present). An estimate — the gateway never sees a bill.`}
        </p>
      </div>
    </Panel>
  )
}
