import { thumbHashToDataURL } from 'thumbhash'

const cache = new Map<string, string>()

export function thumbhashDataUrl(hash: string): string | null {
  const cached = cache.get(hash)
  if (cached) return cached
  try {
    const bytes = Uint8Array.from(atob(hash), (c) => c.charCodeAt(0))
    const url = thumbHashToDataURL(bytes)
    cache.set(hash, url)
    return url
  } catch {
    return null
  }
}
