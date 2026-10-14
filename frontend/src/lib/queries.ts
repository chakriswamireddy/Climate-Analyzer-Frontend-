/** react-query wiring: keys, retry/backoff policy, shared client factory.

 * Policy mirror of docs/03 §6: retry 1s/3s/9s (+20% jitter) up to 3 attempts,
 * only for retryable codes and network errors; never on 404/422; exactly one
 * attempt on 429 after `Retry-After`; `REGION_NOT_READY`/`TIMEOUT` are not
 * retried here — the preparing state polls instead (CF-05). */

import { QueryClient } from '@tanstack/react-query'
import { ApiError } from './api'
import type { AnalysisParams } from './urlParams'

export interface CanonicalAnalysisQuery extends Record<string, string | number> {
  region_id: string
  hazard: string
  start_year: number
  end_year: number
  method: 'ols'
}

export function canonicalQuery(params: AnalysisParams): CanonicalAnalysisQuery {
  return {
    region_id: params.regionId as string,
    hazard: params.hazard,
    start_year: params.startYear,
    end_year: params.endYear,
    method: 'ols',
  }
}

export const QUERY_KEYS = {
  hazards: ['hazards'] as const,
  presets: ['presets'] as const,
  search: (q: string) => ['search', q] as const,
  region: (regionId: string) => ['region', regionId] as const,
  timeseries: (q: CanonicalAnalysisQuery) => ['analysis', 'timeseries', q] as const,
  summary: (q: CanonicalAnalysisQuery) => ['analysis', 'summary', q] as const,
  events: (q: CanonicalAnalysisQuery, page: number) => ['analysis', 'events', q, page] as const,
  analysisRoot: ['analysis'] as const,
}

export function isRateLimitError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 429 || error.code === 'RATE_LIMITED')
}

/** 409 REGION_NOT_READY / 504 TIMEOUT = data still being prepared (CF-05). */
export function isPreparingError(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.code === 'REGION_NOT_READY' || error.code === 'TIMEOUT') &&
    error.status !== 0
  )
}

export function isNotFoundError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.code === 'REGION_NOT_FOUND')
}

/** Deployed free tier sleeps after idle: the first request either cannot be
 * connected at all (status 0) or comes back as 503 while the service boots.
 * Both are indistinguishable from a cold start at the client, so the UI names
 * it and keeps polling instead of showing a dead-end error. A 500 with a real
 * envelope stays a normal error (TC-F-04). */
export function isColdStartError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  return error.isNetwork || error.status === 503 || error.code === 'SERVICE_UNAVAILABLE'
}

export function retryOn(failureCount: number, error: unknown): boolean {
  if (failureCount >= 3) return false
  if (!(error instanceof ApiError)) return failureCount < 3
  if (error.status === 429) return failureCount < 1
  if (isPreparingError(error)) return false // polled, not retried
  return error.retryable
}

export function retryDelayOn(failureCount: number, error: unknown): number {
  if (error instanceof ApiError && error.status === 429) {
    return Math.max(1, error.retryAfterSeconds ?? 5) * 1000
  }
  if (error instanceof ApiError && isPreparingError(error) && error.retryAfterSeconds) {
    return error.retryAfterSeconds * 1000
  }
  const base = [1000, 3000, 9000][Math.min(failureCount, 2)]
  return Math.round(base * (0.8 + Math.random() * 0.4))
}

export interface QueryClientOptions {
  /** Tests speed up failure paths by disabling the retry ladder. */
  retry?: boolean
}

export function makeQueryClient(options: QueryClientOptions = {}): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 60_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: false, // protects the 60/min rate budget (§11)
        retry: options.retry === false ? false : retryOn,
        retryDelay: retryDelayOn,
      },
      mutations: { retry: false },
    },
  })
}

/** CF-05 / CF-13: stop stale analysis requests when params change. With
 * `current`, the canonical query the component is now showing is excluded —
 * so a fetch that starts in the same commit as the change is never cancelled
 * by its predecessor's effect cleanup. */
export function cancelStaleAnalysis(client: QueryClient, current?: CanonicalAnalysisQuery): void {
  void client.cancelQueries({
    queryKey: QUERY_KEYS.analysisRoot,
    predicate: (query) =>
      current === undefined ||
      JSON.stringify(query.queryKey[2] ?? null) !== JSON.stringify(current),
  })
}
