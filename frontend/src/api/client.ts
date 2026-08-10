import type {
  GenerationRequest,
  JobCreated,
  JobList,
  JobView,
  MetricsView,
} from './types'
import { getState, noteCorrelationId, setKey } from '../lib/store'

export class ApiError extends Error {
  code: string
  suggestion: string | null
  correlationId: string | null
  retryAfterS: number | null
  /** epoch ms when retry becomes allowed; fixed at parse time so re-renders cannot reset the countdown */
  retryDeadline: number | null
  status: number

  constructor(args: {
    status: number
    code: string
    message: string
    suggestion?: string | null
    correlationId?: string | null
    retryAfterS?: number | null
  }) {
    super(args.message)
    this.name = 'ApiError'
    this.status = args.status
    this.code = args.code
    this.suggestion = args.suggestion ?? null
    this.correlationId = args.correlationId ?? null
    this.retryAfterS = args.retryAfterS ?? null
    this.retryDeadline =
      this.retryAfterS !== null ? Date.now() + this.retryAfterS * 1000 : null
  }
}

function headers(extra?: Record<string, string>): Record<string, string> {
  const key = getState().key
  return {
    ...(key ? { Authorization: `Bearer ${key}` } : {}),
    ...extra,
  }
}

async function toApiError(res: Response): Promise<ApiError> {
  const retryAfter = res.headers.get('Retry-After')
  let code = `HTTP_${res.status}`
  let message = res.statusText || `Request failed with ${res.status}`
  let suggestion: string | null = null
  let correlationId = res.headers.get('X-Correlation-ID')
  try {
    const body = (await res.json()) as {
      error?: {
        code: string
        message: string
        suggestion?: string | null
        correlation_id?: string | null
      }
    }
    if (body.error) {
      code = body.error.code
      message = body.error.message
      suggestion = body.error.suggestion ?? null
      correlationId = body.error.correlation_id ?? correlationId
    }
  } catch {
    // non-JSON body: keep the status-derived fields
  }
  if (res.status === 401) setKey(null)
  return new ApiError({
    status: res.status,
    code,
    message,
    suggestion,
    correlationId,
    retryAfterS: retryAfter ? Number(retryAfter) : null,
  })
}

const REQUEST_TIMEOUT_MS = 30_000

async function request(path: string, init?: RequestInit): Promise<Response> {
  // timeout aborts throw a DOMException, not an ApiError, so the poll loop's
  // transient-network retry path still applies
  const res = await fetch(path, {
    ...init,
    signal: init?.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  })
  const correlationId = res.headers.get('X-Correlation-ID')
  if (correlationId) noteCorrelationId(correlationId)
  if (!res.ok) throw await toApiError(res)
  return res
}

export interface Submission {
  replayed: boolean
  created: JobCreated | null
  /** populated on an idempotent replay (200 returns the full job) */
  view: JobView | null
}

export async function submitJob(
  body: GenerationRequest,
  idempotencyKey: string,
): Promise<Submission> {
  const res = await request('/v1/jobs', {
    method: 'POST',
    headers: headers({
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
    }),
    body: JSON.stringify(body),
  })
  if (res.status === 200) {
    return { replayed: true, created: null, view: (await res.json()) as JobView }
  }
  return {
    replayed: false,
    created: (await res.json()) as JobCreated,
    view: null,
  }
}

export async function getJob(jobId: string): Promise<JobView> {
  const res = await request(`/v1/jobs/${jobId}`, { headers: headers() })
  return (await res.json()) as JobView
}

export async function listJobs(limit = 100): Promise<JobList> {
  const res = await request(`/v1/jobs?limit=${limit}`, { headers: headers() })
  return (await res.json()) as JobList
}

export async function fetchMetrics(): Promise<MetricsView> {
  const res = await request('/v1/metrics', { headers: headers() })
  return (await res.json()) as MetricsView
}

export async function cancelJob(jobId: string): Promise<JobView> {
  const res = await request(`/v1/jobs/${jobId}/cancel`, {
    method: 'POST',
    headers: headers(),
  })
  return (await res.json()) as JobView
}

/** Image route needs the bearer header, so <img src> cannot be used directly. */
export async function fetchImageBlobUrl(jobId: string): Promise<string> {
  const res = await request(`/v1/jobs/${jobId}/image`, { headers: headers() })
  return URL.createObjectURL(await res.blob())
}

export async function fetchHealth(): Promise<boolean> {
  try {
    const res = await fetch('/health', {
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    })
    return res.ok
  } catch {
    return false
  }
}
