export type JobStatus =
  | 'QUEUED'
  | 'IN_PROGRESS'
  | 'COMPLETED'
  | 'FAILED'
  | 'BLOCKED'
  | 'CANCELLED'
  | 'TIMED_OUT'

export const TERMINAL_STATUSES: ReadonlySet<JobStatus> = new Set([
  'COMPLETED',
  'FAILED',
  'BLOCKED',
  'CANCELLED',
  'TIMED_OUT',
])

export function isTerminal(status: JobStatus): boolean {
  return TERMINAL_STATUSES.has(status)
}

export interface Progress {
  step: number
  total: number
  percent: number
  /** small (≤15kB) base64 JPEG of the current denoising state; some IN_PROGRESS polls only */
  preview_b64?: string
  preview_format?: 'jpeg'
}

export interface JobResult {
  format: 'png' | 'jpeg'
  seed: number
  width: number
  height: number
  model_version: string
  inference_seconds: number
  /** postgres mode */
  image_url?: string
  thumbhash?: string
  /** memory mode */
  image_base64?: string
}

export interface ErrorBody {
  code: string
  message: string
  suggestion?: string | null
  correlation_id?: string | null
}

/** Echo of the accepted generation request; seed present only when caller-fixed. */
export interface RequestEcho {
  prompt: string
  width: number
  height: number
  num_inference_steps: number
  guidance_scale: number
  output_format: 'png' | 'jpeg'
  seed?: number
}

export interface JobView {
  job_id: string
  status: JobStatus
  request: RequestEcho
  progress: Progress | null
  result: JobResult | null
  error: ErrorBody | null
  created_at: string
  updated_at: string
  completed_at: string | null
}

export interface JobCreated {
  job_id: string
  status: JobStatus
  created_at: string
}

export interface JobSummary {
  job_id: string
  status: JobStatus
  prompt: string
  width: number
  height: number
  num_inference_steps: number
  guidance_scale: number
  output_format: 'png' | 'jpeg'
  seed: number | null
  thumbhash: string | null
  image_url: string | null
  model_version: string | null
  inference_seconds: number | null
  error_code: string | null
  created_at: string
  completed_at: string | null
}

export interface JobList {
  jobs: JobSummary[]
}

/** Nearest-rank percentiles over one latency series; null before any sample. */
export interface LatencyPercentiles {
  p50_s: number | null
  p95_s: number | null
  max_s: number | null
}

export interface ThroughputBucket {
  /** UTC hour, ISO */
  hour: string
  completed: number
}

export interface QueueHealthView {
  /** 'ok' | 'unknown' */
  status: string
  in_queue: number | null
  in_progress: number | null
  workers_running: number | null
  workers_idle: number | null
  /** seconds since the cached upstream reading was taken */
  age_s: number | null
  /** the gateway's verdict: true when the reading is too old or absent entirely */
  stale: boolean
  /** the threshold that verdict was made against (HEALTH_MAX_AGE_S) */
  stale_after_s: number
}

export interface ReconcilerView {
  /** 'ok' | 'stalled' | 'unknown' */
  status: string
  last_tick_s: number | null
  /** true when the loop is not known to have ticked within stale_after_s */
  stale: boolean
  /** three RECONCILE_IDLE_INTERVAL_S */
  stale_after_s: number
}

/** GET /v1/metrics — the Operate dashboard's data source. */
export interface MetricsView {
  jobs: {
    /** all seven statuses, all-time in the store */
    by_status: Record<string, number>
    last_hour: number
    active_now: number
  }
  latency: { inference: LatencyPercentiles; wall: LatencyPercentiles }
  /** 24 dense UTC-hour buckets, zeros included */
  throughput: ThroughputBucket[]
  cost: {
    estimated_cost_usd: number
    estimated_cost_usd_per_job: number | null
    /** the input the estimate came from: exec_seconds × rate / 3600 === estimated_cost_usd */
    exec_seconds_in_window: number
    gpu_rate_usd_hr: number
  }
  upstream: { queue: QueueHealthView; reconciler: ReconcilerView }
  /** how many completed jobs the percentile window spans */
  window: number
  completed_in_window: number
  /** oldest completed_at in the window; null iff completed_in_window === 0 */
  window_started_at: string | null
  /** newest completed_at in the window; equals window_started_at when it holds one job */
  window_ended_at: string | null
  generated_at: string
}

export interface GenerationRequest {
  prompt: string
  width: number
  height: number
  num_inference_steps: number
  guidance_scale: number
  seed?: number
  output_format: 'png' | 'jpeg'
}
