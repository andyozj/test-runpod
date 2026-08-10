import { m } from 'motion/react'
import type { JobRun } from '../hooks/useBatch'
import { fmtMs, fmtS } from '../lib/format'
import { COLD_TOOLTIP } from '../lib/bench'
import { CopyAction, CopyValue } from './Copyable'

type AggStage = 'queued' | 'inference' | 'done'

function currentStage(run: JobRun): AggStage {
  if (run.error || run.phase === 'done') return 'done'
  const open = run.stages.find((s) => s.endedAtMs === null)
  if (!open || open.id === 'queued') return 'queued'
  return 'inference'
}

/** Duration a cell spent (or has spent so far) in one stage; null if never entered. */
function stageMs(run: JobRun, id: 'queued' | 'inference'): number | null {
  const stage = run.stages.find((s) => s.id === id)
  if (!stage) return null
  return (stage.endedAtMs ?? run.elapsedMs) - stage.startedAtMs
}

/**
 * One honest aggregate for the whole batch: cells per stage plus the slowest
 * cell's elapsed in that stage. Per-cell percent stays in each cell.
 */
export function BatchTimeline({
  runs,
  prompt,
  batchWallMs,
  kind = 'batch',
  sweepSummary = null,
  onCancelAll,
}: {
  runs: JobRun[]
  prompt: string
  batchWallMs: number | null
  kind?: 'batch' | 'sweep'
  /** `4→3.1s · 12→9.1s · …` once every sweep cell is terminal */
  sweepSummary?: string | null
  onCancelAll: () => void
}) {
  const n = runs.length
  const stagesNow = runs.map(currentStage)
  const inFlight = stagesNow.filter((s) => s !== 'done').length
  const elapsedMs = Math.max(0, ...runs.map((r) => r.elapsedMs))

  const rows: {
    id: AggStage
    count: number
    active: boolean
    detail: string | null
    note: string | null
  }[] = (['queued', 'inference', 'done'] as const).map((id) => {
    const count = stagesNow.filter((s) => s === id).length
    if (id === 'done') {
      return {
        id,
        count,
        active: false,
        detail:
          count === n && batchWallMs !== null
            ? `${kind} wall ${fmtS(batchWallMs)}`
            : null,
        note: null,
      }
    }
    const durations = runs
      .map((r) => stageMs(r, id))
      .filter((ms): ms is number => ms !== null)
    return {
      id,
      count,
      active: count > 0,
      detail:
        durations.length > 0 ? `slowest ${fmtMs(Math.max(...durations))}` : null,
      note:
        runs
          .flatMap((r) => r.stages)
          .find((s) => s.id === id && s.note !== undefined)?.note ?? null,
    }
  })

  return (
    <div
      data-testid="batch-timeline"
      className="panel w-full border border-hairline"
    >
      <div className="flex items-baseline justify-between border-b border-hairline px-3 py-1.5">
        <span className="microlabel">{kind} timeline</span>
        <span className="font-mono text-xs text-ink-label" data-testid="dyn">
          {fmtMs(elapsedMs)}
        </span>
      </div>
      {/* announced only when a cell changes stage, not on second ticks */}
      <span role="status" className="sr-only">
        {rows.map((row) => `${row.id} ${row.count} of ${n}`).join(', ')}
      </span>
      <ol className="flex flex-col px-3 py-1.5">
        {rows.map((row) => (
          <m.li
            key={row.id}
            initial={{ opacity: 0, y: 2 }}
            animate={{ opacity: 1, y: 0 }}
            className="flex items-baseline gap-3 border-l py-1.5 pl-3"
            style={{
              borderColor: row.active
                ? 'var(--color-accent)'
                : 'var(--color-hairline)',
            }}
          >
            <span
              className={`w-20 text-xs ${row.active ? 'text-accent' : 'text-ink-dim'}`}
            >
              {row.id}
            </span>
            <span className="font-mono text-[13px] text-ink" data-testid="dyn">
              {row.count}/{n}
            </span>
            {row.detail && (
              <span className="font-mono text-[11px] text-ink-label" data-testid="dyn">
                {row.detail}
              </span>
            )}
            {row.note && (
              <span
                title={COLD_TOOLTIP}
                className="cursor-help font-mono text-[11px] text-warn"
              >
                {row.note}
              </span>
            )}
          </m.li>
        ))}
      </ol>
      {sweepSummary && (
        <div
          data-testid="sweep-summary"
          className="flex items-start gap-3 border-t border-hairline px-3 py-2"
        >
          <span className="microlabel">sweep</span>
          <span data-testid="dyn" className="min-w-0">
            <CopyValue
              value={sweepSummary}
              className="text-left text-[12px] text-ink"
            />
          </span>
        </div>
      )}
      <div className="flex items-center gap-2 border-t border-hairline px-3 py-2">
        {inFlight > 0 ? (
          <button
            type="button"
            onClick={onCancelAll}
            title={`Cancel all ${inFlight} in-flight jobs`}
            className="btn-ghost danger px-2 py-1 text-xs"
          >
            cancel
          </button>
        ) : (
          <span className="font-mono text-[11px] text-ink-label">
            {kind} settled
          </span>
        )}
        <CopyAction label="copy prompt" text={() => prompt} />
      </div>
    </div>
  )
}
