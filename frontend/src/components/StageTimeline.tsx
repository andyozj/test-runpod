import { m } from 'motion/react'
import type { Stage } from '../hooks/useBatch'
import type { Progress } from '../api/types'
import { fmtMs } from '../lib/format'
import { COLD_TOOLTIP } from '../lib/bench'
import { CopyAction } from './Copyable'

const STAGE_ORDER = ['queued', 'inference', 'finalize'] as const

/** Strip welded beneath the result cell: hairline frame, no top border. */
export function StageTimeline({
  stages,
  progress,
  elapsedMs,
  prompt,
  onCancel,
}: {
  stages: Stage[]
  progress: Progress | null
  elapsedMs: number
  prompt: string
  onCancel: () => void
}) {
  const ordered = [...stages].sort(
    (a, b) => STAGE_ORDER.indexOf(a.id) - STAGE_ORDER.indexOf(b.id),
  )
  const active = ordered.find((s) => s.endedAtMs === null)
  return (
    <>
      {/* throttled announcement: stage transitions and 20% strides, never the second-ticking rows */}
      <span role="status" className="sr-only">
        {stageAnnouncement(active, progress)}
      </span>
      <div
        data-testid="stage-timeline"
        className="panel w-full border border-t-0 border-hairline"
      >
      <div className="flex items-baseline justify-between border-b border-hairline px-3 py-1.5">
        <span className="microlabel">stage timeline</span>
        <span className="font-mono text-xs text-ink-label" data-testid="dyn">
          {fmtMs(elapsedMs)}
        </span>
      </div>
      <ol className="flex flex-col px-3 py-1.5">
        {ordered.map((stage) => {
          const active = stage.endedAtMs === null
          return (
            <m.li
              key={stage.id}
              initial={{ opacity: 0, y: 2 }}
              animate={{ opacity: 1, y: 0 }}
              className="flex items-baseline gap-3 border-l py-1.5 pl-3"
              style={{
                borderColor: active
                  ? 'var(--color-accent)'
                  : 'var(--color-hairline)',
              }}
            >
              <span
                className={`w-20 text-xs ${active ? 'text-accent' : 'text-ink-dim'}`}
              >
                {stage.label}
              </span>
              <span className="font-mono text-[13px] text-ink" data-testid="dyn">
                {active ? liveDetail(stage, progress, elapsedMs) : fmtMs(stage.endedAtMs! - stage.startedAtMs)}
              </span>
              {stage.note && (
                <span
                  title={COLD_TOOLTIP}
                  className="cursor-help font-mono text-[11px] text-warn"
                >
                  {stage.note}
                </span>
              )}
            </m.li>
          )
        })}
      </ol>
      <div className="flex items-center gap-2 border-t border-hairline px-3 py-2">
        <button
          type="button"
          onClick={onCancel}
          className="btn-ghost danger px-2 py-1 text-xs"
        >
          cancel
        </button>
        <CopyAction label="copy prompt" text={() => prompt} />
      </div>
      </div>
    </>
  )
}

/** Screen-reader text that only changes at stage transitions and 20% strides. */
function stageAnnouncement(
  active: Stage | undefined,
  progress: Progress | null,
): string {
  if (!active) return 'done'
  if (active.id === 'inference' && progress) {
    return `inference ${Math.floor(progress.percent / 20) * 20}%`
  }
  return active.label
}

function liveDetail(
  stage: Stage,
  progress: Progress | null,
  elapsedMs: number,
): string {
  const running = fmtMs(elapsedMs - stage.startedAtMs)
  if (stage.id === 'inference' && progress) {
    return `${progress.percent}% · step ${progress.step}/${progress.total} · ${running}`
  }
  return `${running} elapsed`
}
