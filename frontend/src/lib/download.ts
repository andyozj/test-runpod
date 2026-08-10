export function imageFilename(
  seed: number | null,
  width: number,
  height: number,
  ext: string,
): string {
  const seedPart = seed !== null ? `seed${seed}_` : ''
  return `flux_${seedPart}${width}x${height}.${ext}`
}

/** Saves an already-fetched object/data URL; no second authorized fetch. */
export function downloadUrl(url: string, filename: string): void {
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.append(a)
  a.click()
  a.remove()
}
