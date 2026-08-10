import { useSyncExternalStore } from 'react'

export interface Route {
  view: 'generate' | 'gallery' | 'operate'
  /** /jobs/:id — gallery with the detail dialog open on that job */
  jobId: string | null
  /** /gallery?status=FAILED — the ledger opens filtered by that status */
  status: string | null
}

export function parseRoute(pathname: string, search = ''): Route | null {
  const status = new URLSearchParams(search).get('status')
  if (pathname === '/' || pathname === '/generate') {
    return { view: 'generate', jobId: null, status: null }
  }
  if (pathname === '/gallery') return { view: 'gallery', jobId: null, status }
  if (pathname === '/operate') return { view: 'operate', jobId: null, status: null }
  const job = /^\/jobs\/([^/]+)$/.exec(pathname)
  if (job) return { view: 'gallery', jobId: job[1], status }
  return null
}

const listeners = new Set<() => void>()

function emit(): void {
  for (const listener of listeners) listener()
}

let route: Route = normalize()

/** Unknown paths redirect to /generate (replace, so back never re-404s). */
function normalize(): Route {
  const parsed = parseRoute(window.location.pathname, window.location.search)
  if (parsed) return parsed
  window.history.replaceState(null, '', '/generate')
  return { view: 'generate', jobId: null, status: null }
}

/** Paths carry a query string, so identity is pathname + search. */
function here(): string {
  return window.location.pathname + window.location.search
}

window.addEventListener('popstate', () => {
  route = normalize()
  emit()
})

export function navigate(path: string, opts?: { replace?: boolean }): void {
  if (here() === path) return
  // replace keeps the current entry's state (e.g. the dialog push marker)
  if (opts?.replace) window.history.replaceState(window.history.state, '', path)
  else window.history.pushState(null, '', path)
  route = normalize()
  emit()
}

/** history.back() when this session pushed the current entry; else replace with `fallback`. */
export function closeToFallback(fallback: string): void {
  if (window.history.state?.pushedByApp) window.history.back()
  else navigate(fallback, { replace: true })
}

/** Push with a marker so closeToFallback knows back() lands inside the app. */
export function pushMarked(path: string): void {
  if (here() === path) return
  window.history.pushState({ pushedByApp: true }, '', path)
  route = normalize()
  emit()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function getRoute(): Route {
  return route
}

export function useRoute(): Route {
  return useSyncExternalStore(subscribe, getRoute)
}
