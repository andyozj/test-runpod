/** BENCHMARKS.md, steps sweep: 28 steps / 1024², N=10, exec p50. */
export const BENCH_WARM_EXEC_P50_S = 21.8

/** Committed benchmark record count; the session's first job is "data point 157". */
export const BENCH_RECORD_COUNT = 156

/**
 * The envelope every committed warm run fell inside: BENCHMARKS.md steps sweep
 * at 1024², slowest and fastest cell exec p50 (4 steps 3.8 s → 50 steps 38.2 s).
 * Measured, not modelled — the sparkline's density band.
 */
export const BENCH_WARM_EXEC_MIN_S = 3.8
export const BENCH_WARM_EXEC_MAX_S = 38.2

/** BENCHMARKS.md cold-start numbers, quoted wherever a cold queue is flagged. */
export const COLD_TOOLTIP =
  'scale-from-zero: the platform stages the worker and loads ~34GB of weights — measured cold p50 90 s, resume 16 s, warm 0.1 s (BENCHMARKS.md)'

/** Per-image estimate is unchanged for batches: cells run on parallel workers. */
export function generateLabel(batch: number): string {
  return batch > 1 ? `Generate ×${batch} · ~22s warm` : 'Generate · ~22s warm'
}
