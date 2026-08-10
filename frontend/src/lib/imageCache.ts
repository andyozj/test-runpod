import { fetchImageBlobUrl } from '../api/client'

/**
 * jobId → object-URL LRU. Sharing one URL across mounts stops the detail
 * overlay refetching blobs the gallery card already holds; revocation happens
 * on eviction only, never on unmount, so live <img> elements keep their src.
 */
const MAX_ENTRIES = 24

const cache = new Map<string, Promise<string>>()

export function getJobImageUrl(jobId: string): Promise<string> {
  const hit = cache.get(jobId)
  if (hit) {
    cache.delete(jobId)
    cache.set(jobId, hit)
    return hit
  }
  const entry = fetchImageBlobUrl(jobId)
  cache.set(jobId, entry)
  entry.catch(() => cache.delete(jobId))
  if (cache.size > MAX_ENTRIES) {
    const oldest = cache.entries().next().value
    if (oldest) {
      cache.delete(oldest[0])
      oldest[1].then((url) => URL.revokeObjectURL(url)).catch(() => {})
    }
  }
  return entry
}
