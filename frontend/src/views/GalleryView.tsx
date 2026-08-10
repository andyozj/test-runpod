import { useEffect, useMemo, useRef, useState } from 'react'
import { m } from 'motion/react'
import { ApiError, getJob, listJobs } from '../api/client'
import type { JobSummary, JobView } from '../api/types'
import { fmtDateHeading, fmtTimestamp, shortSha } from '../lib/format'
import type { Prefill } from '../lib/params'
import { closeToFallback, navigate, pushMarked } from '../lib/router'
import { noteModelVersion, useAppState } from '../lib/store'
import { JobImage } from '../components/JobImage'
import { Lightbox } from '../components/Lightbox'
import { NoKeyState } from '../components/KeyPopover'
import { CopyAction, CopyValue } from '../components/Copyable'
import { downloadUrl, imageFilename } from '../lib/download'

interface Filter {
  kind: 'size' | 'seed' | 'model' | 'status'
  value: string
}

const STATUS_COLOR: Record<string, string> = {
  COMPLETED: 'text-ok',
  FAILED: 'text-err',
  BLOCKED: 'text-warn',
  TIMED_OUT: 'text-err',
  CANCELLED: 'text-ink-dim',
  QUEUED: 'text-accent',
  IN_PROGRESS: 'text-accent',
}

/** Which `.code-chip` tone a non-completed status carries. */
function statusTone(status: string): string {
  if (status === 'BLOCKED') return 'warn'
  if (status === 'CANCELLED' || status === 'QUEUED' || status === 'IN_PROGRESS')
    return 'neutral'
  return ''
}

function filterValue(job: JobSummary, kind: Filter['kind']): string | null {
  switch (kind) {
    case 'size':
      return `${job.width}×${job.height}`
    case 'seed':
      return job.seed !== null ? String(job.seed) : null
    case 'model':
      return shortSha(job.model_version)
    case 'status':
      return job.status
  }
}

export function GalleryView({
  active,
  detailJobId,
  statusFilter,
  onPrefill,
}: {
  active: boolean
  /** /jobs/:id — the route owns which detail dialog is open */
  detailJobId: string | null
  /** ?status=FAILED — Operate links here filtered by a status */
  statusFilter: string | null
  onPrefill: (p: Prefill) => void
}) {
  const { key } = useAppState()
  const [jobs, setJobs] = useState<JobSummary[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [filter, setFilter] = useState<Filter | null>(
    statusFilter ? { kind: 'status', value: statusFilter } : null,
  )

  // arriving with ?status= adopts that filter; leaving the param off (e.g. a
  // pushed detail route) must not clear a filter the user is already in
  useEffect(() => {
    if (statusFilter) setFilter({ kind: 'status', value: statusFilter })
  }, [statusFilter])

  useEffect(() => {
    if (!active || !key) return
    let alive = true
    listJobs(100)
      .then((list) => {
        if (!alive) return
        setJobs(list.jobs)
        setLoadError(null)
        const latest = list.jobs.find(
          (j) => j.status === 'COMPLETED' && j.model_version,
        )
        if (latest?.model_version) noteModelVersion(latest.model_version)
      })
      .catch((err: unknown) => {
        if (alive)
          setLoadError(err instanceof ApiError ? err.message : 'Network failure.')
      })
    return () => {
      alive = false
    }
  }, [active, key])

  const filtered = useMemo(() => {
    if (!jobs) return null
    if (!filter) return jobs
    return jobs.filter((j) => filterValue(j, filter.kind) === filter.value)
  }, [jobs, filter])

  const groups = useMemo(() => {
    if (!filtered) return []
    const byDate = new Map<string, JobSummary[]>()
    for (const job of filtered) {
      const day = job.created_at.slice(0, 10)
      const bucket = byDate.get(day) ?? []
      bucket.push(job)
      byDate.set(day, bucket)
    }
    return [...byDate.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1))
  }, [filtered])

  // detail dialog derives from the route: back/forward and deep links just work
  const detail = useMemo(
    () =>
      detailJobId && filtered
        ? (filtered.find((j) => j.job_id === detailJobId) ?? null)
        : null,
    [detailJobId, filtered],
  )
  const detailIndex = useMemo(
    () =>
      detail && filtered
        ? filtered.findIndex((j) => j.job_id === detail.job_id)
        : -1,
    [detail, filtered],
  )

  // deep link to a job the ledger does not know: fall back to the ledger
  useEffect(() => {
    if (active && detailJobId && jobs && !jobs.some((j) => j.job_id === detailJobId)) {
      navigate('/gallery', { replace: true })
    }
  }, [active, detailJobId, jobs])

  if (!key) return <NoKeyState />

  return (
    <div className="mx-auto h-full max-w-[1440px] overflow-auto px-6 py-4">
      <div className="mb-4 flex items-center gap-3">
        <span className="microlabel">reproducibility ledger</span>
        {jobs && (
          <span className="font-mono text-xs text-ink-label">
            {filtered?.length}/{jobs.length} jobs
          </span>
        )}
        {filter && (
          <button
            type="button"
            onClick={() => {
              setFilter(null)
              // the URL carried the filter in; clearing it must clear the URL too
              if (statusFilter) navigate('/gallery', { replace: true })
            }}
            className="cursor-pointer rounded border border-accent-dim px-2 py-0.5 font-mono text-[11px] text-accent"
          >
            {filter.kind} {filter.value} ×
          </button>
        )}
      </div>

      {loadError && (
        <p className="font-mono text-xs text-err">{loadError}</p>
      )}
      {jobs === null && !loadError && <SkeletonGrid />}
      {jobs && jobs.length === 0 && (
        <p className="font-mono text-xs text-ink-label">
          no jobs yet — the first generation lands here
        </p>
      )}

      {groups.map(([day, dayJobs], groupIndex) => {
        const before = groups
          .slice(0, groupIndex)
          .reduce((n, [, list]) => n + list.length, 0)
        return (
          <section key={day} className="mb-6">
            <h2
              aria-label={`${fmtDateHeading(day)}, ${dayJobs.length} ${dayJobs.length === 1 ? 'job' : 'jobs'}`}
              className="mb-2 border-b border-hairline pb-1 font-mono text-xs text-ink-dim"
            >
              <span aria-hidden>{fmtDateHeading(day)}</span>
              <span aria-hidden className="ml-2 text-ink-label">
                · {dayJobs.length} {dayJobs.length === 1 ? 'job' : 'jobs'}
              </span>
            </h2>
            <div className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-3">
              {dayJobs.map((job, i) => (
                <Card
                  key={job.job_id}
                  job={job}
                  mountIndex={before + i}
                  onOpen={() => pushMarked(`/jobs/${job.job_id}`)}
                  onChip={(kind, value) =>
                    setFilter((prev) =>
                      prev && prev.kind === kind && prev.value === value
                        ? null
                        : { kind, value },
                    )
                  }
                  onPrefill={onPrefill}
                />
              ))}
            </div>
          </section>
        )
      })}

      {detail && filtered && detailIndex >= 0 && (
        <DetailOverlay
          summary={detail}
          index={detailIndex}
          total={filtered.length}
          onNavigate={(delta) => {
            const next = filtered[detailIndex + delta]
            // replace, not push: arrowing through the list is one history entry
            if (next) navigate(`/jobs/${next.job_id}`, { replace: true })
          }}
          onClose={() => closeToFallback('/gallery')}
          onPrefill={onPrefill}
        />
      )}
    </div>
  )
}

/**
 * The JOB's full recorded config — steps, guidance, format, batch 1 — never
 * the rail's current state: a rerun must reproduce what actually ran.
 */
function prefillOf(job: JobSummary, sameSeed: boolean): Prefill {
  return {
    prompt: job.prompt,
    width: job.width,
    height: job.height,
    steps: job.num_inference_steps,
    guidance: job.guidance_scale,
    format: job.output_format,
    batch: 1,
    mode: 'batch',
    seed: sameSeed ? job.seed : null,
    seedMode: sameSeed && job.seed !== null ? 'lock' : 'dice',
  }
}

/** One chip grammar app-wide: lowercase mono `label value`, label in the label tier. */
function Chip({
  label,
  onClick,
  children,
  title,
}: {
  label?: string
  onClick?: () => void
  children: React.ReactNode
  title?: string
}) {
  const body = (
    <>
      {label && <span className="text-ink-label">{label} </span>}
      {children}
    </>
  )
  if (!onClick) {
    return (
      <span
        title={title}
        className="h-[22px] rounded border border-hairline px-1.5 py-0.5 font-mono text-[11px] text-ink-dim"
      >
        {body}
      </span>
    )
  }
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className="h-[22px] cursor-pointer rounded border border-hairline-strong px-1.5 py-0.5 font-mono text-[11px] text-ink-dim hover:border-[oklch(1_0_0/24%)] hover:text-ink"
    >
      {body}
    </button>
  )
}

/** Card-shaped placeholders while the ledger loads: elevation pulse, no spinner. */
function SkeletonGrid() {
  return (
    <div
      aria-hidden
      className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] items-start gap-3"
    >
      {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
        <div
          key={i}
          className="overflow-hidden rounded-[6px] border border-hairline"
        >
          <div className="skeleton w-full" style={{ aspectRatio: '1 / 1' }} />
          <div className="flex flex-col gap-2 border-t border-hairline bg-surface px-3 py-2.5">
            <div className="skeleton h-3 w-3/4 rounded" />
            <div className="skeleton h-12 w-full rounded" />
            <div className="skeleton h-6 w-1/2 rounded" />
          </div>
        </div>
      ))}
    </div>
  )
}

function Card({
  job,
  mountIndex,
  onOpen,
  onChip,
  onPrefill,
}: {
  job: JobSummary
  mountIndex: number
  onOpen: () => void
  onChip: (kind: Filter['kind'], value: string) => void
  onPrefill: (p: Prefill) => void
}) {
  const sha = shortSha(job.model_version)
  const batch = useAppState().sessionBatches[job.job_id]
  return (
    <m.article
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25, delay: mountIndex * 0.03 }}
      className="group raised-lit flex flex-col overflow-hidden rounded-[6px] border border-hairline bg-surface transition-colors hover:border-hairline-strong"
    >
      <button
        type="button"
        onClick={onOpen}
        title="Open details"
        aria-label={`Open details: ${job.prompt}`}
        className="relative block w-full cursor-zoom-in"
      >
        {/* uniform square thumbnail: rows stay flush. The true aspect is stated
            by the size chip and rendered in the detail overlay and lightbox. */}
        <JobImage
          jobId={job.image_url ? job.job_id : null}
          thumbhash={job.thumbhash}
          alt={job.prompt}
          style={{ aspectRatio: '1 / 1' }}
          className="w-full"
        />
        {job.status !== 'COMPLETED' && !job.image_url && (
          <span
            aria-hidden
            className="absolute inset-0 flex items-center justify-center"
          >
            <span className={`code-chip ${statusTone(job.status)}`}>
              {job.error_code ?? job.status}
            </span>
          </span>
        )}
      </button>
      <div className="flex flex-col gap-1.5 border-t border-hairline px-3 py-2.5">
        <p className="truncate font-mono text-[11px] text-ink-dim" title={job.prompt}>
          {job.prompt}
        </p>
        <div className="card-chips">
          <Chip
            label="size"
            onClick={() => onChip('size', `${job.width}×${job.height}`)}
          >
            {job.width}×{job.height}
          </Chip>
          {job.seed !== null && (
            <Chip label="seed" onClick={() => onChip('seed', String(job.seed))}>
              {job.seed}
            </Chip>
          )}
          {sha && (
            <Chip
              label="model"
              onClick={() => onChip('model', sha)}
              title={job.model_version ?? ''}
            >
              @{sha}
            </Chip>
          )}
          {job.inference_seconds !== null && (
            <Chip label="inf">{job.inference_seconds.toFixed(1)} s</Chip>
          )}
          {batch && (
            <Chip
              label={batch.sweepLabel ? 'sweep' : 'batch'}
              title={
                batch.sweepLabel
                  ? `one of ${batch.size} sweep cells run together this session — this cell's varied value: ${batch.sweepLabel}`
                  : `one of ${batch.size} jobs submitted together this session — client-side grouping, not stored by the API`
              }
            >
              {batch.sweepLabel
                ? `${batch.stem} · ${batch.sweepLabel}`
                : `${batch.stem} · ×${batch.size}`}
            </Chip>
          )}
          {job.status !== 'COMPLETED' && (
            <button
              type="button"
              onClick={() => onChip('status', job.status)}
              className={`code-chip ${statusTone(job.status)} h-[22px] cursor-pointer`}
            >
              {job.error_code ?? job.status}
            </button>
          )}
        </div>
        <div className="hover-reveal flex gap-1.5 opacity-0 transition-[opacity,translate] duration-150 group-focus-within:translate-y-0 group-focus-within:opacity-100 group-hover:translate-y-0 group-hover:opacity-100 motion-safe:translate-y-0.5">
          <button
            type="button"
            disabled={job.seed === null}
            onClick={() => onPrefill(prefillOf(job, true))}
            title="Rerun with the same seed — reproduces this image"
            className="btn-ghost whitespace-nowrap px-2 py-1 text-[11px]"
          >
            rerun seed
          </button>
          <button
            type="button"
            onClick={() => onPrefill(prefillOf(job, false))}
            title="Rerun the prompt with a new random seed"
            className="btn-ghost whitespace-nowrap px-2 py-1 text-[11px]"
          >
            reroll · new seed
          </button>
        </div>
      </div>
    </m.article>
  )
}

/** `org/model@3de6…2eb21` — keeps the row one line; click still copies the full value. */
function midTruncate(value: string): string {
  const at = value.indexOf('@')
  if (at < 0 || value.length - at < 14) return value
  const sha = value.slice(at + 1)
  return `${value.slice(0, at + 1)}${sha.slice(0, 4)}…${sha.slice(-4)}`
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <tr className="border-b border-hairline align-baseline">
      <td className="microlabel w-20 py-1.5 pr-3">{label}</td>
      <td className="py-1.5 font-mono text-[13px] text-ink">{children}</td>
    </tr>
  )
}

function DetailOverlay({
  summary,
  index,
  total,
  onNavigate,
  onClose,
  onPrefill,
}: {
  summary: JobSummary
  index: number
  total: number
  onNavigate: (delta: 1 | -1) => void
  onClose: () => void
  onPrefill: (p: Prefill) => void
}) {
  const [view, setView] = useState<JobView | null>(null)
  const [imgSrc, setImgSrc] = useState<string | null>(null)
  const [lightbox, setLightbox] = useState(false)
  const dialogRef = useRef<HTMLDialogElement>(null)

  // native showModal: focus trap, inert backdrop, Esc via cancel; focus goes
  // back to the opener explicitly — the native restore is unreliable when
  // close() runs during React unmount
  useEffect(() => {
    const dialog = dialogRef.current
    const opener = document.activeElement
    if (dialog && !dialog.open) dialog.showModal()
    return () => {
      dialog?.close()
      if (opener instanceof HTMLElement) opener.focus()
    }
  }, [])

  useEffect(() => {
    setView(null)
    setImgSrc(null)
    setLightbox(false)
    let alive = true
    getJob(summary.job_id)
      .then((v) => {
        if (alive) setView(v)
      })
      .catch(() => {
        // summary fields still render
      })
    return () => {
      alive = false
    }
  }, [summary.job_id])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (lightbox) return // the lightbox owns the keyboard while open
      if (e.key === 'ArrowRight') onNavigate(1)
      else if (e.key === 'ArrowLeft') onNavigate(-1)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onNavigate, lightbox])

  const result = view?.result ?? null
  const wallS =
    summary.completed_at !== null
      ? (new Date(summary.completed_at).getTime() -
          new Date(summary.created_at).getTime()) /
        1000
      : null
  const correlationId = view?.error?.correlation_id ?? null

  const asJson = () =>
    JSON.stringify(view ?? summary, null, 2)

  const asCurl = () => {
    // the job's full recorded request — steps and guidance included — so the
    // command actually reproduces this generation
    const body: Record<string, unknown> = {
      prompt: summary.prompt,
      width: summary.width,
      height: summary.height,
      num_inference_steps: summary.num_inference_steps,
      guidance_scale: summary.guidance_scale,
      ...(summary.seed !== null ? { seed: summary.seed } : {}),
      output_format: summary.output_format,
    }
    const json = JSON.stringify(body).replace(/'/g, `'\\''`)
    return [
      `curl -X POST ${window.location.origin}/v1/jobs \\`,
      `  -H "Authorization: Bearer $GATEWAY_KEY" \\`,
      `  -H "Content-Type: application/json" \\`,
      `  -d '${json}'`,
    ].join('\n')
  }

  return (
    <dialog
      ref={dialogRef}
      aria-label="Job details"
      onCancel={(e) => {
        // Esc targets the topmost modal, so the open lightbox closes first;
        // the target guard drops re-propagated cancels from nested dialogs
        if (e.target !== e.currentTarget) return
        e.preventDefault()
        onClose()
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
      className="modal-shell z-30 flex items-center justify-center bg-[oklch(0_0_0/65%)] p-4 backdrop-blur-[8px] sm:p-6"
    >
      <div
        style={{ boxShadow: '0 24px 64px oklch(0 0 0 / 55%)' }}
        className="raised-lit flex max-h-full w-full max-w-4xl flex-col gap-0 overflow-y-auto rounded-[6px] border border-[oklch(1_0_0/12%)] bg-overlay sm:flex-row sm:overflow-hidden"
      >
        <div className="flex items-center justify-center bg-bg p-4 sm:w-1/2">
          <button
            type="button"
            onClick={() => imgSrc && setLightbox(true)}
            disabled={!imgSrc}
            title="View at full size"
            aria-label="View image at full size"
            className="relative block w-full cursor-zoom-in disabled:cursor-default"
          >
            <JobImage
              key={summary.job_id}
              jobId={summary.image_url ? summary.job_id : null}
              thumbhash={summary.thumbhash}
              imageBase64={result?.image_base64}
              format={result?.format ?? 'png'}
              alt={summary.prompt}
              eager
              onSrc={setImgSrc}
              className="w-full rounded-[4px]"
              style={{ aspectRatio: `${summary.width} / ${summary.height}` }}
            />
          </button>
        </div>
        <div className="flex flex-col p-4 sm:w-1/2 sm:overflow-auto">
          <div className="mb-3 flex items-center justify-between">
            <div className="flex items-baseline gap-3">
              <span className="microlabel">job metadata</span>
              <span
                title="← → to step through the filtered list"
                className="font-mono text-[11px] text-ink-label"
              >
                {index + 1} / {total}
              </span>
            </div>
            <button
              type="button"
              onClick={onClose}
              title="Close (Esc)"
              aria-label="Close"
              className="cursor-pointer rounded border border-hairline-strong px-2 py-0.5 font-mono text-xs text-ink-dim hover:text-ink"
            >
              esc
            </button>
          </div>
          <table className="w-full border-collapse">
            <tbody>
              <Row label="prompt">{summary.prompt}</Row>
              <Row label="status">
                <span className={STATUS_COLOR[summary.status] ?? ''}>
                  {summary.status}
                </span>
              </Row>
              {(view?.error || summary.error_code) && (
                <Row label="error">
                  <span className={`code-chip ${statusTone(summary.status)}`}>
                    {view?.error?.code ?? summary.error_code}
                  </span>
                  {view?.error?.message && (
                    <span className="ml-2 text-ink-dim">{view.error.message}</span>
                  )}
                </Row>
              )}
              <Row label="seed">
                {summary.seed !== null ? (
                  <CopyValue value={String(summary.seed)} />
                ) : (
                  '—'
                )}
              </Row>
              <Row label="size">{`${summary.width} × ${summary.height} px`}</Row>
              <Row label="steps">{summary.num_inference_steps}</Row>
              <Row label="guidance">{summary.guidance_scale.toFixed(1)}</Row>
              <Row label="format">{summary.output_format}</Row>
              <Row label="model">
                {summary.model_version ? (
                  <CopyValue
                    value={summary.model_version}
                    display={midTruncate(summary.model_version)}
                    title="Copy the full model revision"
                    className="whitespace-nowrap text-left"
                  />
                ) : (
                  '—'
                )}
              </Row>
              <Row label="inference">
                {summary.inference_seconds !== null
                  ? `${summary.inference_seconds.toFixed(1)} s`
                  : '—'}
              </Row>
              <Row label="wall">
                {wallS !== null ? `${wallS.toFixed(1)} s` : '—'}
              </Row>
              <Row label="created">{fmtTimestamp(summary.created_at)}</Row>
              <Row label="completed">{fmtTimestamp(summary.completed_at)}</Row>
              <Row label="corr">
                {correlationId ? <CopyValue value={correlationId} /> : '—'}
              </Row>
              <Row label="job id">
                <CopyValue value={summary.job_id} className="break-all text-left" />
              </Row>
            </tbody>
          </table>
          <div className="mt-4 flex flex-wrap gap-1.5">
            <CopyAction
              label="copy as json"
              text={asJson}
              title="Copy the full job record as JSON"
            />
            <CopyAction
              label="copy as curl"
              text={asCurl}
              title="reproduces this generation — full recorded request, steps and guidance included"
            />
            {imgSrc && (
              <button
                type="button"
                onClick={() =>
                  downloadUrl(
                    imgSrc,
                    imageFilename(
                      summary.seed,
                      summary.width,
                      summary.height,
                      result?.format ?? 'png',
                    ),
                  )
                }
                title={`Save ${imageFilename(summary.seed, summary.width, summary.height, result?.format ?? 'png')}`}
                className="btn-ghost px-2 py-1 text-xs"
              >
                download {result?.format ?? 'png'}
              </button>
            )}
            <button
              type="button"
              disabled={summary.seed === null}
              onClick={() => onPrefill(prefillOf(summary, true))}
              title="Rerun with the same seed — reproduces this image"
              className="btn-ghost px-2 py-1 text-xs"
            >
              rerun seed
            </button>
            <button
              type="button"
              onClick={() => onPrefill(prefillOf(summary, false))}
              title="Rerun the prompt with a new random seed"
              className="btn-ghost px-2 py-1 text-xs"
            >
              reroll · new seed
            </button>
          </div>
        </div>
      </div>
      {lightbox && imgSrc && (
        <Lightbox
          src={imgSrc}
          width={summary.width}
          height={summary.height}
          seed={summary.seed}
          onClose={() => setLightbox(false)}
        />
      )}
    </dialog>
  )
}
