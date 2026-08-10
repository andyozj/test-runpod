export function fmtMs(ms: number): string {
  return `${Math.round(ms).toLocaleString('en-US')} ms`
}

export function fmtS(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`
}

export function shortSha(modelVersion: string | null | undefined): string | null {
  if (!modelVersion) return null
  const at = modelVersion.indexOf('@')
  if (at < 0) return null
  return modelVersion.slice(at + 1, at + 8)
}

export function fmtDateHeading(iso: string): string {
  // date-only strings parse as UTC midnight; construct locally so the heading
  // never shows the previous day in negative-offset timezones
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number)
  return new Date(y, m - 1, d).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
  })
}

export function fmtTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toISOString().replace('T', ' ').replace(/\.\d+Z$/, 'Z')
}
