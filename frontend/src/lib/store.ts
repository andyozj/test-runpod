import { useSyncExternalStore } from 'react'

const KEY_STORAGE = 'gateway-key'

export interface SessionBatch {
  /** first 4 chars of the batch id; client-side only, never sent on the wire */
  stem: string
  size: number
  /** sweep cells only: this job's varied value, e.g. "4 steps" */
  sweepLabel?: string
}

export interface WallTime {
  ms: number
  /** queued >3s: likely a cold start — rendered hollow on the sparkline */
  cold: boolean
}

export interface AppState {
  key: string | null
  /** header key popover, openable from the pre-key empty state */
  keyPopoverOpen: boolean
  lastCorrelationId: string | null
  modelVersion: string | null
  /** completed wall times this session */
  wallTimes: WallTime[]
  /** last /health poll; null until the first response */
  gatewayOk: boolean | null
  /** job_id → batch grouping for jobs submitted together this session */
  sessionBatches: Record<string, SessionBatch>
}

let state: AppState = {
  key: localStorage.getItem(KEY_STORAGE),
  keyPopoverOpen: false,
  lastCorrelationId: null,
  modelVersion: null,
  wallTimes: [],
  gatewayOk: null,
  sessionBatches: {},
}

const listeners = new Set<() => void>()

function emit(next: AppState) {
  state = next
  for (const listener of listeners) listener()
}

export function getState(): AppState {
  return state
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function setKey(key: string | null): void {
  if (key) localStorage.setItem(KEY_STORAGE, key)
  else localStorage.removeItem(KEY_STORAGE)
  emit({ ...state, key })
}

/** Whether a key is held. The caller never learns its `api_key_id`: the
 * gateway resolves that server-side from the secret and returns it in no
 * response, so any id rendered here would be a guess. */
export function keyId(key: string | null): string | null {
  return key ? 'set' : null
}

export function noteCorrelationId(id: string): void {
  if (id !== state.lastCorrelationId) emit({ ...state, lastCorrelationId: id })
}

export function noteModelVersion(version: string): void {
  if (version !== state.modelVersion) emit({ ...state, modelVersion: version })
}

export function noteWallTime(ms: number, cold: boolean): void {
  emit({ ...state, wallTimes: [...state.wallTimes, { ms, cold }] })
}

export function setKeyPopoverOpen(open: boolean): void {
  if (open !== state.keyPopoverOpen) emit({ ...state, keyPopoverOpen: open })
}

export function noteBatch(
  jobIds: (string | null)[],
  stem: string,
  sweepLabels?: string[],
): void {
  const next = { ...state.sessionBatches }
  const size = jobIds.filter((id) => id !== null).length
  if (size < 2) return
  jobIds.forEach((id, i) => {
    if (id !== null) next[id] = { stem, size, sweepLabel: sweepLabels?.[i] }
  })
  emit({ ...state, sessionBatches: next })
}

export function noteHealth(ok: boolean): void {
  if (ok !== state.gatewayOk) emit({ ...state, gatewayOk: ok })
}

export function useAppState(): AppState {
  return useSyncExternalStore(subscribe, getState)
}
