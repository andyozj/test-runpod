import {
  BENCH_RECORD_COUNT,
  BENCH_WARM_EXEC_MAX_S,
  BENCH_WARM_EXEC_MIN_S,
  BENCH_WARM_EXEC_P50_S,
} from '../lib/bench'
import { fmtS } from '../lib/format'
import type { WallTime } from '../lib/store'

/** The rail card's inner content width: 304 rail − 24 rail pad − 2 border − 32 section pad. */
const W = 246
const H = 40
const PAD = 4

const CAPTION_TOOLTIP = `band = the committed warm-exec envelope, ${BENCH_WARM_EXEC_MIN_S}–${BENCH_WARM_EXEC_MAX_S} s across the BENCHMARKS.md steps sweep at 1024²; line = ${BENCH_WARM_EXEC_P50_S} s p50 at 28 steps. ${BENCH_RECORD_COUNT} measured runs — your session's jobs append from ${BENCH_RECORD_COUNT + 1}.`

/** Session wall times plotted against the committed benchmark envelope. */
export function Sparkline({ wallTimes }: { wallTimes: WallTime[] }) {
  const empty = wallTimes.length === 0
  return (
    <figure
      className="flex flex-col gap-1.5 font-mono text-[11px] text-ink-label"
      data-testid={empty ? undefined : 'dyn'}
    >
      <Strip wallTimes={wallTimes} />
      <figcaption title={CAPTION_TOOLTIP} className="cursor-help">
        {empty
          ? `no session jobs yet · next is data point ${BENCH_RECORD_COUNT + 1}`
          : `p50 ref ${BENCH_WARM_EXEC_P50_S} s · data point ${
              BENCH_RECORD_COUNT + wallTimes.length
            } · last ${fmtS(wallTimes[wallTimes.length - 1].ms)}`}
      </figcaption>
    </figure>
  )
}

function Strip({ wallTimes }: { wallTimes: WallTime[] }) {
  const p50Ms = BENCH_WARM_EXEC_P50_S * 1000
  const bandLoMs = BENCH_WARM_EXEC_MIN_S * 1000
  const bandHiMs = BENCH_WARM_EXEC_MAX_S * 1000
  const maxMs = Math.max(bandHiMs, ...wallTimes.map((w) => w.ms)) * 1.06
  const y = (ms: number) => H - PAD - (ms / maxMs) * (H - PAD * 2)
  const x = (i: number) =>
    wallTimes.length === 1
      ? W / 2
      : PAD + (i / (wallTimes.length - 1)) * (W - PAD * 2)
  const points = wallTimes.map((w, i) => ({ cx: x(i), cy: y(w.ms), cold: w.cold }))
  const yRef = y(p50Ms)

  return (
    <svg
      width={W}
      height={H}
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={
        wallTimes.length === 0
          ? `Benchmark envelope ${BENCH_WARM_EXEC_MIN_S} to ${BENCH_WARM_EXEC_MAX_S} seconds, p50 ${BENCH_WARM_EXEC_P50_S} seconds, no session jobs yet`
          : `Session wall times, ${wallTimes.length} jobs, against the benchmark envelope, p50 ${BENCH_WARM_EXEC_P50_S}s`
      }
      className="shrink-0"
    >
      {/* the committed distribution as a density band, not a single line */}
      <rect
        x={0}
        y={y(bandHiMs)}
        width={W}
        height={Math.max(1, y(bandLoMs) - y(bandHiMs))}
        fill="oklch(1 0 0 / 12%)"
      />
      {/* envelope edges, so the band reads as a measured interval not a fill */}
      {[y(bandHiMs), y(bandLoMs)].map((yEdge, i) => (
        <line
          key={i}
          x1={0}
          x2={W}
          y1={yEdge}
          y2={yEdge}
          stroke="oklch(1 0 0 / 16%)"
          strokeWidth={1}
        />
      ))}
      <line
        x1={0}
        x2={W}
        y1={yRef}
        y2={yRef}
        stroke="oklch(1 0 0 / 38%)"
        strokeWidth={1}
      />
      <text
        x={W - 4}
        y={yRef - 4}
        textAnchor="end"
        fontSize={10}
        fontFamily="var(--font-mono)"
        fill="var(--color-ink-label)"
      >
        p50
      </text>
      {points.length > 1 && (
        <polyline
          points={points.map((p) => `${p.cx},${p.cy}`).join(' ')}
          fill="none"
          stroke="var(--color-accent-dim)"
          strokeWidth={1}
        />
      )}
      {points.map((p, i) =>
        // cold runs (queued >3s) are hollow: same series, different regime
        p.cold ? (
          <rect
            key={i}
            data-cold
            x={p.cx - 2.5}
            y={p.cy - 2.5}
            width={5}
            height={5}
            fill="var(--color-bg)"
            stroke="var(--color-accent)"
            strokeWidth={1}
          />
        ) : (
          <rect
            key={i}
            x={p.cx - 1.5}
            y={p.cy - 1.5}
            width={3}
            height={3}
            fill="var(--color-accent)"
          />
        ),
      )}
    </svg>
  )
}
