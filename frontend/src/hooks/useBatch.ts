import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, cancelJob, getJob, submitJob } from '../api/client'
import { isTerminal, type GenerationRequest, type JobView } from '../api/types'
import { noteBatch, noteModelVersion, noteWallTime } from '../lib/store'

export type StageId = 'queued' | 'inference' | 'finalize'

export interface Stage {
  id: StageId
  label: string
  /** ms since submit */
  startedAtMs: number
  /** ms since submit; null while open */
  endedAtMs: number | null
  note?: string
}

export interface PreviewFrame {
  /** data: URL of the latest denoising preview */
  src: string
  step: number
  total: number
  /** increments per distinct frame; crossfade key */
  seq: number
}

export interface JobRun {
  phase: 'idle' | 'submitting' | 'running' | 'done'
  job: JobView | null
  error: ApiError | null
  stages: Stage[]
  /** submit → terminal, ms; null until terminal (or for replays, never observed) */
  wallMs: number | null
  /** live ms since submit while running */
  elapsedMs: number
  replayed: boolean
  /** latest denoising preview; transient — dropped on terminal per the wire contract */
  preview: PreviewFrame | null
  /** the submitted request; a cell retry resubmits exactly this */
  request: GenerationRequest | null
  /** Idempotency-Key sent with the POST; reused on retry while no job id is known */
  idempotencyKey: string | null
}

export const IDLE_RUN: JobRun = {
  phase: 'idle',
  job: null,
  error: null,
  stages: [],
  wallMs: null,
  elapsedMs: 0,
  replayed: false,
  preview: null,
  request: null,
  idempotencyKey: null,
}

const COLD_START_NOTE = 'likely cold start — platform staging can take minutes'

/** Queued longer than this marks the run's wall time as a cold measurement.
 *
 * Warm dispatch against the live endpoint measured 6-10s queued with three
 * idle workers (2026-08-10), so the earlier 3s threshold labelled every real
 * job cold. 20s sits above warm dispatch and below the 89.9s cold p50 in
 * BENCHMARKS.md. */
export const COLD_QUEUE_MS = 20_000

/** Duration a completed run spent queued; 0 if never observed. */
export function queuedMsOf(run: JobRun): number {
  const queued = run.stages.find((s) => s.id === 'queued')
  if (!queued || queued.endedAtMs === null) return 0
  return queued.endedAtMs - queued.startedAtMs
}

/** Duration a completed run spent in inference (open stages fall back to elapsed). */
export function inferenceMsOf(run: JobRun): number | null {
  const stage = run.stages.find((s) => s.id === 'inference')
  if (!stage) return null
  return (stage.endedAtMs ?? run.elapsedMs) - stage.startedAtMs
}

function fastMock(): boolean {
  return (
    import.meta.env.VITE_MOCK === '1' && localStorage.getItem('MOCK_FAST') === '1'
  )
}

/** Queued longer than this gets the cold-start note; compressed in fast-mock runs. */
function coldNoteAfterMs(): number {
  return fastMock() ? 1500 : 30_000
}

/**
 * Fast-mock jobs finish in 1-4s, so 1s polls would quantize per-cell wall
 * times into uselessness — a steps sweep's whole lesson lives in that spread.
 */
function pollDelayMs(elapsedMs: number): number {
  if (fastMock()) return 250
  return elapsedMs > 60_000 ? 2000 : 1000
}

/** One job's submit→poll→terminal state machine; the hook runs N of these. */
class CellRunner {
  run: JobRun = IDLE_RUN
  jobId: string | null = null

  private notify: () => void
  private timer: ReturnType<typeof setTimeout> | null = null
  private runId = 0
  private submittedAt = 0
  private stages: Stage[] = []
  private preview: PreviewFrame | null = null
  private previewB64: string | null = null
  private previewSeq = 0
  private disposed = false
  private idemKey: string | null = null
  /** runId whose cancel arrived before the POST resolved; honored once the job id exists */
  private cancelRequestedForRun: number | null = null

  constructor(notify: () => void) {
    this.notify = notify
  }

  dispose(): void {
    this.disposed = true
    this.runId += 1
    if (this.timer) clearTimeout(this.timer)
  }

  private setRun(next: JobRun): void {
    this.run = next
    this.notify()
  }

  private elapsed(): number {
    return performance.now() - this.submittedAt
  }

  private closeStage(id: StageId, at: number): void {
    this.stages = this.stages.map((s) =>
      s.id === id && s.endedAtMs === null ? { ...s, endedAtMs: at } : s,
    )
  }

  private openStage(id: StageId, label: string, at: number): void {
    if (this.stages.some((s) => s.id === id)) return
    this.stages = [...this.stages, { id, label, startedAtMs: at, endedAtMs: null }]
  }

  /** Derive the stage timeline from what the poll actually observed. */
  private applyView(view: JobView, runId: number): void {
    if (this.disposed || runId !== this.runId) return
    const at = this.elapsed()
    const terminal = isTerminal(view.status)

    if (view.status === 'QUEUED' && at > coldNoteAfterMs()) {
      this.stages = this.stages.map((s) =>
        s.id === 'queued' && s.endedAtMs === null
          ? { ...s, note: COLD_START_NOTE }
          : s,
      )
    }
    const started =
      view.status === 'IN_PROGRESS' || view.progress !== null || terminal
    if (started) {
      this.closeStage('queued', at)
    }
    if (view.status === 'IN_PROGRESS' || view.progress !== null) {
      this.openStage('inference', 'inference', at)
    }
    if (view.progress?.percent === 100 && !terminal) {
      this.closeStage('inference', at)
      this.openStage('finalize', 'finalize', at)
    }
    const p = view.progress
    if (!terminal && p?.preview_b64 && p.preview_b64 !== this.previewB64) {
      this.previewB64 = p.preview_b64
      this.previewSeq += 1
      this.preview = {
        src: `data:image/${p.preview_format ?? 'jpeg'};base64,${p.preview_b64}`,
        step: p.step,
        total: p.total,
        seq: this.previewSeq,
      }
    }
    if (terminal) {
      this.preview = null
      this.previewB64 = null
      this.stages = this.stages.map((s) =>
        s.endedAtMs === null ? { ...s, endedAtMs: at } : s,
      )
      if (view.result?.model_version) noteModelVersion(view.result.model_version)
      if (view.status === 'COMPLETED') {
        const queued = this.stages.find((s) => s.id === 'queued')
        const queuedMs = queued ? (queued.endedAtMs ?? at) - queued.startedAtMs : 0
        noteWallTime(at, queuedMs > COLD_QUEUE_MS)
      }
      this.setRun({
        ...this.run,
        phase: 'done',
        job: view,
        stages: this.stages,
        wallMs: at,
        elapsedMs: at,
        preview: null,
      })
      return
    }
    this.setRun({
      ...this.run,
      phase: 'running',
      job: view,
      stages: this.stages,
      elapsedMs: at,
      preview: this.preview,
    })
    this.timer = setTimeout(() => {
      void this.poll(view.job_id, runId)
    }, pollDelayMs(at))
  }

  private async poll(jobId: string, runId: number): Promise<void> {
    if (this.disposed || runId !== this.runId) return
    try {
      const view = await getJob(jobId)
      this.applyView(view, runId)
    } catch (err) {
      if (this.disposed || runId !== this.runId) return
      if (err instanceof ApiError) {
        this.setRun({ ...this.run, phase: 'done', error: err })
      } else {
        // transient network failure: keep polling
        this.timer = setTimeout(() => {
          void this.poll(jobId, runId)
        }, 2000)
      }
    }
  }

  async submit(body: GenerationRequest): Promise<void> {
    if (this.timer) clearTimeout(this.timer)
    const runId = ++this.runId
    this.submittedAt = performance.now()
    this.stages = [{ id: 'queued', label: 'queued', startedAtMs: 0, endedAtMs: null }]
    this.preview = null
    this.previewB64 = null
    // key reuse is scoped to "the create may have happened without us learning
    // the job id" (429 shed, network drop): a retry then replays instead of
    // duplicating the job. Once a job id is known, a resubmit is a new job.
    if (this.idemKey === null || this.jobId !== null) {
      this.idemKey = crypto.randomUUID()
    }
    this.jobId = null
    this.setRun({
      ...IDLE_RUN,
      phase: 'submitting',
      stages: this.stages,
      request: body,
      idempotencyKey: this.idemKey,
    })
    try {
      const submission = await submitJob(body, this.idemKey)
      if (this.disposed || runId !== this.runId) return
      if (submission.replayed && submission.view) {
        const view = submission.view
        this.jobId = view.job_id
        if (isTerminal(view.status)) {
          // Replay of a finished job: nothing was observed, so no stages, no wall time.
          this.stages = []
          if (view.result?.model_version) noteModelVersion(view.result.model_version)
          this.setRun({
            ...IDLE_RUN,
            phase: 'done',
            job: view,
            replayed: true,
            request: body,
            idempotencyKey: this.idemKey,
          })
          return
        }
        this.setRun({ ...this.run, phase: 'running', replayed: true })
        void this.poll(view.job_id, runId)
        return
      }
      const jobId = submission.created?.job_id
      if (!jobId) return
      this.jobId = jobId
      this.setRun({ ...this.run, phase: 'running' })
      if (this.cancelRequestedForRun === runId) {
        this.cancelRequestedForRun = null
        void this.cancel()
        return
      }
      this.timer = setTimeout(() => {
        void this.poll(jobId, runId)
      }, pollDelayMs(0))
    } catch (err) {
      if (this.disposed || runId !== this.runId) return
      this.setRun({
        ...this.run,
        phase: 'done',
        error:
          err instanceof ApiError
            ? err
            : new ApiError({
                status: 0,
                code: 'NETWORK_ERROR',
                message: err instanceof Error ? err.message : 'Network failure.',
                suggestion: 'Check the connection and retry.',
              }),
      })
    }
  }

  async cancel(): Promise<void> {
    // done: nothing to cancel; a later explicit retry starts a fresh run
    if (this.run.phase === 'done') return
    const jobId = this.run.job?.job_id ?? this.jobId
    if (!jobId) {
      // idle = its submit hasn't started yet (batch loop): target the coming run
      this.cancelRequestedForRun =
        this.run.phase === 'idle' ? this.runId + 1 : this.runId
      return
    }
    try {
      const view = await cancelJob(jobId)
      this.applyView(view, this.runId)
    } catch {
      // the poll loop reports the final state either way
    }
  }
}

/**
 * N parallel jobs, one runner each (existing poll cadence per cell).
 * The single-job path is the N=1 case of the same machinery.
 */
export function useBatch() {
  const [runs, setRuns] = useState<JobRun[]>([])
  const runnersRef = useRef<CellRunner[]>([])
  const aliveRef = useRef(true)

  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
      runnersRef.current.forEach((r) => r.dispose())
    }
  }, [])

  const sync = useCallback(() => {
    if (!aliveRef.current) return
    setRuns(runnersRef.current.map((r) => r.run))
  }, [])

  const submitBatch = useCallback(
    async (bodies: GenerationRequest[], sweepLabels?: string[]) => {
      runnersRef.current.forEach((r) => r.dispose())
      runnersRef.current = bodies.map(() => new CellRunner(sync))
      sync()
      // sequential POSTs keep cell order deterministic; polling still runs in parallel
      for (let i = 0; i < bodies.length; i++) {
        await runnersRef.current[i].submit(bodies[i])
      }
      if (bodies.length > 1) {
        const stem = crypto.randomUUID().slice(0, 4)
        noteBatch(
          runnersRef.current.map((r) => r.jobId),
          stem,
          sweepLabels,
        )
      }
    },
    [sync],
  )

  const cancelAll = useCallback(() => {
    runnersRef.current.forEach((r) => void r.cancel())
  }, [])

  const retryCell = useCallback((index: number) => {
    const runner = runnersRef.current[index]
    if (runner?.run.request) void runner.submit(runner.run.request)
  }, [])

  const reset = useCallback(() => {
    runnersRef.current.forEach((r) => r.dispose())
    runnersRef.current = []
    setRuns([])
  }, [])

  // batch wall time: submit → last terminal, from observed cells only
  const allDone = runs.length > 0 && runs.every((r) => r.phase === 'done')
  const observed = runs
    .map((r) => r.wallMs)
    .filter((w): w is number => w !== null)
  const batchWallMs = allDone && observed.length > 0 ? Math.max(...observed) : null

  return { runs, submitBatch, cancelAll, retryCell, reset, batchWallMs }
}
