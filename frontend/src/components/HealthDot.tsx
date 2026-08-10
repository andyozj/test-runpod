export function HealthDot({ healthy }: { healthy: boolean | null }) {
  const color =
    healthy === null
      ? 'var(--color-ink-label)'
      : healthy
        ? 'var(--color-ok)'
        : 'var(--color-err)'
  return (
    <span
      aria-label={
        healthy === null ? 'gateway unknown' : healthy ? 'gateway ok' : 'gateway error'
      }
      role="img"
      className="inline-block size-1.5 rounded-full"
      style={{ backgroundColor: color }}
    />
  )
}
