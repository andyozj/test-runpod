import { useCopied } from '../hooks/useCopied'

export function CopyValue({
  value,
  display,
  title = 'Copy',
  className = '',
}: {
  value: string
  display?: string
  title?: string
  className?: string
}) {
  const [copied, copy] = useCopied()
  return (
    <button
      type="button"
      onClick={() => copy(value)}
      title={title}
      className={`cursor-pointer font-mono hover:text-ink ${className}`}
    >
      {copied ? 'copied' : (display ?? value)}
    </button>
  )
}

export function CopyAction({
  label,
  text,
  title,
  className = '',
}: {
  label: string
  text: () => string
  title?: string
  className?: string
}) {
  const [copied, copy] = useCopied()
  return (
    <button
      type="button"
      title={title}
      onClick={() => copy(text())}
      className={`btn-ghost px-2 py-1 text-xs ${className}`}
    >
      {copied ? 'copied' : label}
    </button>
  )
}
