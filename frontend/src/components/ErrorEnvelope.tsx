import { useEffect, useState } from 'react'
import { CopyValue } from './Copyable'

export interface EnvelopeData {
  code: string
  message: string
  suggestion: string | null
  correlationId: string | null
  /** epoch ms when retry becomes allowed; fixed when the error was parsed */
  retryDeadline: number | null
}

const PROMPT_CODES = new Set(['PROMPT_BLOCKED', 'IMAGE_BLOCKED', 'INVALID_PROMPT'])

function secondsLeft(deadline: number | null): number | null {
  if (deadline === null) return null
  return Math.max(0, Math.ceil((deadline - Date.now()) / 1000))
}

export function ErrorEnvelope({
  error,
  onRetry,
  onEditPrompt,
  compact = false,
  retryLabel,
}: {
  error: EnvelopeData
  onRetry: () => void
  onEditPrompt: () => void
  /** in-cell rendering at batch scale: tighter spacing, smaller type */
  compact?: boolean
  retryLabel?: string
}) {
  const [remaining, setRemaining] = useState(() => secondsLeft(error.retryDeadline))

  // keyed on the error's identity fields, not the object: parents re-allocate
  // envelope objects per render and must not reset the countdown
  useEffect(() => {
    setRemaining(secondsLeft(error.retryDeadline))
    if (error.retryDeadline === null) return
    const tick = setInterval(() => {
      const left = secondsLeft(error.retryDeadline)
      setRemaining(left)
      if (left !== null && left <= 0) clearInterval(tick)
    }, 250)
    return () => clearInterval(tick)
  }, [error.retryDeadline, error.correlationId])

  const promptFix = PROMPT_CODES.has(error.code)
  const counting = remaining !== null && remaining > 0
  const actionLabel = counting
    ? `retry in ${remaining}s`
    : promptFix
      ? 'change prompt'
      : (retryLabel ?? 'retry')

  return (
    <div
      data-testid="error-envelope"
      role="alert"
      className={
        compact
          ? 'w-full max-w-md p-2.5'
          : 'panel w-full max-w-md rounded-[6px] border border-hairline p-4'
      }
    >
      <div className="flex items-center gap-2">
        <span className="code-chip">{error.code}</span>
      </div>
      <p className={compact ? 'mt-2 text-xs text-ink' : 'mt-3 text-sm text-ink'}>
        {error.message}
      </p>
      {error.suggestion && (
        <p
          className={`font-mono text-ink-dim ${compact ? 'mt-1 text-[11px]' : 'mt-1 text-xs'}`}
        >
          {error.suggestion}
        </p>
      )}
      <div
        className={`flex items-center gap-3 ${compact ? 'mt-2.5 flex-wrap gap-y-1.5' : 'mt-4'}`}
      >
        <button
          type="button"
          disabled={counting}
          onClick={promptFix ? onEditPrompt : onRetry}
          data-testid={counting ? 'dyn' : undefined}
          className={`btn-primary whitespace-nowrap text-xs ${compact ? 'px-2.5 py-1' : 'px-3 py-1.5'}`}
        >
          {actionLabel}
        </button>
        {error.correlationId && (
          <span
            className="flex items-baseline gap-1.5 text-xs text-ink-dim"
            data-testid="dyn"
          >
            <span className="microlabel">corr</span>
            <CopyValue
              value={error.correlationId}
              display={error.correlationId.slice(0, 8)}
              className="text-xs text-ink-dim"
            />
          </span>
        )}
      </div>
    </div>
  )
}
