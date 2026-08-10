import { useEffect, useRef, useState } from 'react'

const HUD_H = 34
const PAD = 24
const STEP = 1.2
const PAN = 60

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v))
}

/** Full-viewport image inspector: fit ↔ 100% zoom, drag/arrow pan, Esc closes. */
export function Lightbox({
  src,
  width,
  height,
  seed,
  onClose,
}: {
  src: string
  width: number
  height: number
  seed: number | null
  onClose: () => void
}) {
  const [vp, setVp] = useState({ w: window.innerWidth, h: window.innerHeight })
  const [scale, setScale] = useState<number | null>(null) // null = fit
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const [dragging, setDragging] = useState(false)
  const drag = useRef<{ x: number; y: number; ox: number; oy: number; moved: boolean } | null>(null)
  const ref = useRef<HTMLDialogElement>(null)
  const zoomRef = useRef<(dir: 1 | -1) => void>(() => {})

  const fit = Math.min((vp.w - PAD * 2) / width, (vp.h - HUD_H - PAD * 2) / height, 1)
  const current = scale ?? fit
  const zoomed = current > fit + 0.001

  const clampOffset = (off: { x: number; y: number }, s: number) => {
    const mx = Math.max(0, (width * s - vp.w) / 2)
    const my = Math.max(0, (height * s - (vp.h - HUD_H)) / 2)
    return { x: clamp(off.x, -mx, mx), y: clamp(off.y, -my, my) }
  }

  const zoomTo = (next: number) => {
    const s = clamp(next, fit, 1)
    if (s <= fit + 0.001) {
      setScale(null)
      setOffset({ x: 0, y: 0 })
    } else {
      setScale(s)
      setOffset((o) => clampOffset(o, s))
    }
  }
  const toggle = () => zoomTo(zoomed ? fit : 1)
  zoomRef.current = (dir) => zoomTo(current * (dir === 1 ? STEP : 1 / STEP))

  // native showModal: focus trap via inert background, Esc via cancel; focus
  // returns to the zoom trigger explicitly — the native restore is unreliable
  // when close() runs during React unmount
  useEffect(() => {
    const dialog = ref.current
    const opener = document.activeElement
    if (dialog && !dialog.open) dialog.showModal()
    const onResize = () => setVp({ w: window.innerWidth, h: window.innerHeight })
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      dialog?.close()
      if (opener instanceof HTMLElement) opener.focus()
    }
  }, [])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      zoomRef.current(e.deltaY < 0 ? 1 : -1)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'z' || e.key === 'Z') toggle()
      else if (e.key === '+' || e.key === '=') zoomTo(current * STEP)
      else if (e.key === '-') zoomTo(current / STEP)
      else if (e.key.startsWith('Arrow')) {
        e.preventDefault()
        e.stopPropagation()
        if (!zoomed) return
        const dx = e.key === 'ArrowLeft' ? PAN : e.key === 'ArrowRight' ? -PAN : 0
        const dy = e.key === 'ArrowUp' ? PAN : e.key === 'ArrowDown' ? -PAN : 0
        setOffset((o) => clampOffset({ x: o.x + dx, y: o.y + dy }, current))
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  })

  const backdrop = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) onClose()
  }

  return (
    <dialog
      ref={ref}
      aria-label="Image at full size"
      onCancel={(e) => {
        // topmost modal receives Esc first: the lightbox closes before the
        // detail overlay underneath it. React re-propagates cancel to ancestor
        // dialogs, so stop it here.
        e.preventDefault()
        e.stopPropagation()
        onClose()
      }}
      onClick={backdrop}
      className="modal-shell z-50 overflow-hidden bg-bg/95 outline-none"
    >
      <div
        onClick={backdrop}
        style={{ paddingBottom: HUD_H }}
        className="absolute inset-0 flex items-center justify-center"
      >
        <img
          src={src}
          alt="Full-size generated image"
          draggable={false}
          onPointerDown={(e) => {
            e.currentTarget.setPointerCapture(e.pointerId)
            drag.current = { x: e.clientX, y: e.clientY, ox: offset.x, oy: offset.y, moved: false }
            setDragging(true)
          }}
          onPointerMove={(e) => {
            const d = drag.current
            if (!d) return
            const dx = e.clientX - d.x
            const dy = e.clientY - d.y
            if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true
            if (zoomed) setOffset(clampOffset({ x: d.ox + dx, y: d.oy + dy }, current))
          }}
          onPointerUp={(e) => {
            e.currentTarget.releasePointerCapture(e.pointerId)
            const d = drag.current
            drag.current = null
            setDragging(false)
            if (d && !d.moved) toggle()
          }}
          style={{
            width: width * current,
            height: height * current,
            maxWidth: 'none',
            touchAction: 'none',
            transform: `translate(${offset.x}px, ${offset.y}px)`,
          }}
          className={zoomed ? (dragging ? 'cursor-grabbing' : 'cursor-grab') : 'cursor-zoom-in'}
        />
      </div>
      <div
        style={{ height: HUD_H }}
        className="absolute inset-x-0 bottom-0 flex items-center gap-4 border-t border-hairline bg-surface/95 px-4 font-mono text-xs text-ink-dim"
      >
        <span>zoom {Math.round(current * 100)}%</span>
        <span>
          {width} × {height} px
        </span>
        {seed !== null && <span>seed {seed}</span>}
        <span className="ml-auto text-ink-label">click / z 100% · drag pan · +/− zoom</span>
        <button
          type="button"
          onClick={onClose}
          title="Close (Esc)"
          className="cursor-pointer rounded border border-hairline-strong px-2 py-0.5 text-ink-dim hover:text-ink"
        >
          esc
        </button>
      </div>
    </dialog>
  )
}
