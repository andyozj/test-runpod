import { useEffect, useRef, useState } from 'react'
import { m } from 'motion/react'
import { ApiError } from '../api/client'
import { getJobImageUrl } from '../lib/imageCache'
import { thumbhashDataUrl } from '../lib/thumb'

type Phase = 'placeholder' | 'loaded' | 'evicted' | 'error'

/** Thumbhash placeholder crossfading into the authorized image fetch. */
export function JobImage({
  jobId,
  thumbhash,
  imageBase64,
  format = 'png',
  alt,
  className = '',
  style,
  eager = false,
  onSrc,
  underlaySrc,
}: {
  jobId?: string | null
  thumbhash?: string | null
  imageBase64?: string | null
  format?: string
  alt: string
  className?: string
  style?: React.CSSProperties
  eager?: boolean
  /** fires with the displayable URL (data or object) once the image is available */
  onSrc?: (src: string) => void
  /** last denoising preview, shown over the thumbhash so the final crossfades from it */
  underlaySrc?: string | null
}) {
  const [src, setSrc] = useState<string | null>(
    imageBase64 ? `data:image/${format};base64,${imageBase64}` : null,
  )
  const [phase, setPhase] = useState<Phase>(src ? 'loaded' : 'placeholder')
  const [visible, setVisible] = useState(eager)
  const ref = useRef<HTMLDivElement>(null)
  const onSrcRef = useRef(onSrc)
  onSrcRef.current = onSrc

  useEffect(() => {
    if (src) onSrcRef.current?.(src)
  }, [src])

  useEffect(() => {
    if (eager || visible) return
    const el = ref.current
    if (!el) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) setVisible(true)
      },
      { rootMargin: '200px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [eager, visible])

  useEffect(() => {
    if (!visible || src || !jobId) return
    let alive = true
    // the module cache owns the object URLs; unmount must not revoke a URL
    // other mounts may still be rendering
    getJobImageUrl(jobId)
      .then((blobUrl) => {
        if (!alive) return
        setSrc(blobUrl)
        setPhase('loaded')
      })
      .catch((err: unknown) => {
        if (!alive) return
        setPhase(err instanceof ApiError && err.status === 410 ? 'evicted' : 'error')
      })
    return () => {
      alive = false
    }
  }, [visible, src, jobId])

  const placeholder = thumbhash ? thumbhashDataUrl(thumbhash) : null

  return (
    <div
      ref={ref}
      style={style}
      className={`relative overflow-hidden bg-raised ${className}`}
    >
      {placeholder && (
        <img
          src={placeholder}
          alt=""
          aria-hidden
          className="absolute inset-0 h-full w-full object-cover"
        />
      )}
      {underlaySrc && (
        <img
          src={underlaySrc}
          alt=""
          aria-hidden
          data-testid="preview-frame"
          className="absolute inset-0 h-full w-full object-cover"
        />
      )}
      {src && (
        <m.img
          src={src}
          alt={alt}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.4 }}
          className="relative h-full w-full object-cover"
        />
      )}
      {phase === 'evicted' && (
        <div className="absolute inset-0 flex items-center justify-center">
          <span className="font-mono text-xs text-ink-label">
            image evicted · 410
          </span>
        </div>
      )}
      {phase === 'error' && (
        <div className="absolute inset-0 flex items-center justify-center">
          <span className="font-mono text-xs text-ink-label">
            image unavailable
          </span>
        </div>
      )}
    </div>
  )
}
