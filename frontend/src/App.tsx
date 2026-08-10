import { useCallback, useEffect, useState } from 'react'
import { LazyMotion, MotionConfig, domAnimation } from 'motion/react'
import { fetchHealth } from './api/client'
import { GenerateView } from './views/GenerateView'
import { GalleryView } from './views/GalleryView'
import { OperateView } from './views/OperateView'
import { KeyPopover } from './components/KeyPopover'
import { HealthDot } from './components/HealthDot'
import { CopyValue } from './components/Copyable'
import { keyId, noteHealth, useAppState } from './lib/store'
import { navigate, useRoute } from './lib/router'
import { shortSha } from './lib/format'
import type { Prefill } from './lib/params'

function useHealthPoll(): void {
  useEffect(() => {
    let alive = true
    const check = () => {
      void fetchHealth().then((healthy) => {
        if (alive) noteHealth(healthy)
      })
    }
    check()
    const timer = setInterval(check, 30_000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])
}

const TABS = [
  { path: '/generate', view: 'generate', label: 'generate' },
  { path: '/gallery', view: 'gallery', label: 'ledger' },
  { path: '/operate', view: 'operate', label: 'operate' },
] as const

function App() {
  const route = useRoute()
  const view = route.view
  const [railOpen, setRailOpen] = useState(false)
  const [prefill, setPrefill] = useState<Prefill | null>(null)
  const [prefillNonce, setPrefillNonce] = useState(0)
  const { key, lastCorrelationId, modelVersion, gatewayOk: healthy } = useAppState()
  useHealthPoll()

  const handlePrefill = useCallback((p: Prefill) => {
    setPrefill(p)
    setPrefillNonce((n) => n + 1)
    navigate('/generate')
  }, [])

  const sha = shortSha(modelVersion)

  return (
    <LazyMotion features={domAnimation} strict>
      {/* reduced-motion users keep opacity crossfades, lose transforms */}
      <MotionConfig reducedMotion="user">
      <div className="flex h-svh flex-col">
        <a href="#main" className="skip-link">
          skip to content
        </a>
        <div aria-hidden className="vignette" />
        <div aria-hidden className="grain" />
        <header className="flex shrink-0 items-center gap-2 border-b border-hairline px-3 py-2 sm:gap-6 sm:px-4">
          <span className="hidden flex-col sm:flex">
            <span className="font-mono text-sm tracking-tight">
              flux<span className="text-accent">.</span>instrument
            </span>
            <span className="hidden text-[11px] leading-tight text-ink-dim md:block">
              a measuring instrument for FLUX.1-dev on RunPod serverless
            </span>
          </span>
          <nav className="flex gap-1" aria-label="Views">
            {TABS.map((tab) => (
              <a
                key={tab.path}
                href={tab.path}
                onClick={(e) => {
                  e.preventDefault()
                  navigate(tab.path)
                }}
                aria-current={view === tab.view ? 'page' : undefined}
                className={`relative cursor-pointer rounded px-2 py-1 text-sm capitalize sm:px-3 ${
                  view === tab.view ? 'bg-raised text-ink' : 'text-ink-dim hover:text-ink'
                }`}
              >
                {tab.label}
                {view === tab.view && (
                  <span
                    aria-hidden
                    className="absolute inset-x-2 -bottom-[9px] h-px bg-accent"
                  />
                )}
              </a>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-2 sm:gap-3">
            {view === 'generate' && (
              <button
                type="button"
                aria-expanded={railOpen}
                aria-controls="params-rail"
                onClick={() => setRailOpen((v) => !v)}
                className="cursor-pointer rounded border border-hairline-strong px-2 py-1 font-mono text-xs text-ink-dim hover:text-ink lg:hidden"
              >
                params
              </button>
            )}
            <HealthDot healthy={healthy} />
            <KeyPopover />
          </div>
        </header>

        <main id="main" className="dotgrid relative min-h-0 flex-1">
          <div className={view === 'generate' ? 'h-full' : 'hidden'}>
            <GenerateView
              prefill={prefill}
              prefillNonce={prefillNonce}
              railOpen={railOpen}
              onCloseRail={() => setRailOpen(false)}
            />
          </div>
          <div className={view === 'gallery' ? 'h-full' : 'hidden'}>
            <GalleryView
              active={view === 'gallery'}
              detailJobId={route.jobId}
              statusFilter={route.status}
              onPrefill={handlePrefill}
            />
          </div>
          <div className={view === 'operate' ? 'h-full' : 'hidden'}>
            <OperateView active={view === 'operate'} />
          </div>
        </main>

        <footer className="flex shrink-0 items-center gap-5 overflow-x-auto border-t border-hairline px-4 py-1.5 font-mono text-xs text-ink-label">
          <span className="flex items-baseline gap-1.5" data-testid="dyn">
            <span className="microlabel">corr</span>
            {lastCorrelationId ? (
              <CopyValue
                value={lastCorrelationId}
                display={lastCorrelationId.slice(0, 8)}
                title="last response's X-Correlation-ID — quote it in a bug report"
                className="text-xs text-ink-dim"
              />
            ) : (
              <span>—</span>
            )}
          </span>
          <span className="flex items-center gap-1.5">
            <span className="microlabel">gateway</span>
            <HealthDot healthy={healthy} />
            <span>{healthy === null ? '—' : healthy ? 'ok' : 'err'}</span>
          </span>
          <span className="flex items-baseline gap-1.5">
            <span className="microlabel">model</span>
            {sha && modelVersion ? (
              <CopyValue
                value={modelVersion}
                display={`@${sha}`}
                title="model revision (git sha) — pinned, verifiable"
                className="text-xs text-ink-dim"
              />
            ) : (
              <span>—</span>
            )}
          </span>
          <span className="flex items-baseline gap-1.5">
            <span className="microlabel">key</span>
            <span className={key ? '' : 'text-warn'}>
              {key ? keyId(key) : 'not set'}
            </span>
          </span>
          {import.meta.env.VITE_MOCK === '1' && (
            <span
              className="flex items-baseline gap-1.5"
              title="running against the in-browser mock, not a live endpoint"
            >
              <span className="microlabel">mode</span>
              <span>mock</span>
            </span>
          )}
        </footer>
      </div>
      </MotionConfig>
    </LazyMotion>
  )
}

export default App
