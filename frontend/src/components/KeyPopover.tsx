import { useEffect, useRef, useState } from 'react'
import { keyId, setKey, setKeyPopoverOpen, useAppState } from '../lib/store'

const DEMO_KEY = 'demo:local-development-key'

export function KeyPopover() {
  const { key, keyPopoverOpen: open } = useAppState()
  const [draft, setDraft] = useState('')
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  // button-driven closes (Esc, save, clear) return focus to the trigger;
  // outside clicks leave focus where the pointer put it
  const close = (restoreFocus: boolean) => {
    setKeyPopoverOpen(false)
    if (restoreFocus) triggerRef.current?.focus()
  }

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setKeyPopoverOpen(false)
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setKeyPopoverOpen(false)
        triggerRef.current?.focus()
      }
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={rootRef} className="relative">
      <button
        ref={triggerRef}
        type="button"
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls={open ? 'key-popover' : undefined}
        onClick={() => {
          setDraft('')
          setKeyPopoverOpen(!open)
        }}
        className={`cursor-pointer rounded border px-2 py-1 font-mono text-xs ${
          key
            ? 'border-hairline-strong text-ink-dim hover:text-ink'
            : 'border-warn/50 text-warn'
        }`}
      >
        {key ? `key ${keyId(key)}` : 'set key'}
      </button>
      {open && (
        <div
          id="key-popover"
          style={{ boxShadow: '0 12px 40px oklch(0.1 0.03 70 / 45%)' }}
          className="raised-lit absolute right-0 top-full z-20 mt-1 w-72 rounded-[6px] border border-hairline bg-overlay p-3"
        >
          <span className="microlabel">gateway api key</span>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              if (draft.trim()) {
                setKey(draft.trim())
                close(true)
              }
            }}
            className="mt-2 flex gap-1.5"
          >
            <input
              type="password"
              aria-label="Gateway API key"
              placeholder="key_id:secret"
              autoFocus
              className="w-full rounded border border-hairline-strong bg-raised px-2 py-1 font-mono text-xs text-ink"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
            />
            <button
              type="submit"
              className="btn-primary px-2 py-1 text-xs"
            >
              save
            </button>
          </form>
          <p className="mt-2 text-[11px] leading-relaxed text-ink-label">
            Stored in localStorage, sent as{' '}
            <span className="font-mono">Authorization: Bearer</span>. Cleared on
            401. The demo key is printed beside the deployment URL in the repo
            README.
          </p>
          {key && (
            <button
              type="button"
              onClick={() => {
                setKey(null)
                close(true)
              }}
              className="mt-2 cursor-pointer rounded border border-hairline-strong px-2 py-1 font-mono text-[11px] text-ink-dim hover:text-err"
            >
              clear key
            </button>
          )}
        </div>
      )}
    </div>
  )
}

export function NoKeyState() {
  return (
    <div className="flex h-full items-center justify-center">
      <div className="max-w-sm border border-dashed border-hairline-strong bg-surface/60 p-6 text-center">
        <span className="microlabel">no api key</span>
        <p className="mt-2 font-mono text-xs leading-relaxed text-ink-dim">
          Every request is authorized — paste a gateway key to start measuring.
        </p>
        <div className="mt-3 flex flex-col items-center gap-1.5">
          <button
            type="button"
            onClick={() => setKeyPopoverOpen(true)}
            className="btn-primary px-3 py-1.5 text-xs"
          >
            set key
          </button>
          <button
            type="button"
            title="local dev only — the gateway compose stack accepts this key"
            onClick={() => setKey(DEMO_KEY)}
            className="btn-ghost px-2.5 py-1 text-[11px]"
          >
            use demo key · local dev only
          </button>
        </div>
        <p className="mt-3 text-[11px] leading-relaxed text-ink-label">
          The deployed demo key is printed beside the deployment URL in the repo
          README.
        </p>
      </div>
    </div>
  )
}
