import type { ThroughputBucket } from '../api/types'

/**
 * Bar geometry only — every label is HTML, which lets the plot stretch to its
 * panel with `preserveAspectRatio="none"` without smearing type. Strokes carry
 * `vector-effect` so hairlines stay hairlines under the same stretch.
 */
const W = 720
const H = 100
const SLOT = W / 24
const BAR = SLOT - 9
const PAD_T = 6
/** Zero hours keep their slot: a stub on the axis, never a gap. */
const STUB = 1.5

function utcHour(iso: string): number {
  return new Date(iso).getUTCHours()
}

/**
 * Completions per UTC hour over the last 24 h. Same grammar as the session
 * sparkline: amber marks, hairline axis, label-tier annotation.
 */
export function ThroughputBars({ buckets }: { buckets: ThroughputBucket[] }) {
  const total = buckets.reduce((n, b) => n + b.completed, 0)
  const peak = Math.max(1, ...buckets.map((b) => b.completed))
  const plotH = H - PAD_T
  const heightOf = (n: number) => (n === 0 ? STUB : Math.max(2, (n / peak) * plotH))

  return (
    <figure className="flex min-h-[150px] flex-1 flex-col justify-end gap-1.5">
      <span className="font-mono text-[11px] text-ink-label">
        {total === 0 ? 'peak —' : `peak ${peak}/h`}
      </span>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={
          total === 0
            ? 'Hourly completions over the last 24 hours: none'
            : `Hourly completions over the last 24 hours, ${total} total, peak ${peak} in one hour`
        }
        className="max-h-[260px] w-full flex-1"
      >
        {/* peak gridline: the only y reference, labeled above the plot */}
        <line
          x1={0}
          x2={W}
          y1={PAD_T}
          y2={PAD_T}
          stroke="oklch(1 0 0 / 10%)"
          strokeWidth={1}
          strokeDasharray="2 4"
          vectorEffect="non-scaling-stroke"
        />
        {buckets.map((bucket, i) => {
          const h = heightOf(bucket.completed)
          return (
            <rect
              key={bucket.hour}
              x={i * SLOT + (SLOT - BAR) / 2}
              y={H - h}
              width={BAR}
              height={h}
              fill={
                bucket.completed === 0 ? 'oklch(1 0 0 / 12%)' : 'var(--color-accent)'
              }
            >
              <title>{`${bucket.hour} · ${bucket.completed} completed`}</title>
            </rect>
          )
        })}
        <line
          x1={0}
          x2={W}
          y1={H}
          y2={H}
          stroke="oklch(1 0 0 / 16%)"
          strokeWidth={1}
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <div
        aria-hidden
        style={{
          gridTemplateColumns: `repeat(${buckets.length || 24}, minmax(0, 1fr))`,
        }}
        className="grid font-mono text-[10px] text-ink-label"
      >
        {buckets.map((bucket, i) => (
          <span key={bucket.hour} className="text-center">
            {i === buckets.length - 1
              ? 'now'
              : utcHour(bucket.hour) % 6 === 0
                ? String(utcHour(bucket.hour)).padStart(2, '0')
                : ''}
          </span>
        ))}
      </div>
      <figcaption className="font-mono text-[11px] text-ink-label">
        {total === 0
          ? 'no completions in the last 24 h — every hour is an empty slot until a job finishes'
          : `${total} completed in 24 h · hours are UTC`}
      </figcaption>
    </figure>
  )
}
