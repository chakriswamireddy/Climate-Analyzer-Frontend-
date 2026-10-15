/** Thin fetch wrapper: §7.2 error envelope → typed ApiError (docs/03 §6).

 * Every request carries the 30 s client timeout required by CF-13; a
 * timeout surfaces as a retryable `TIMEOUT` envelope-shaped error instead of
 * a stuck spinner, while an abort caused by the caller's own `AbortSignal`
 * (react-query cancelling a stale query) is re-thrown untouched so the query
 * layer can treat it as a cancellation rather than a failure.
 */

import type { ErrorEnvelope } from './types'

const DEFAULT_TIMEOUT_MS = 30_000

export interface ApiErrorInit {
  code: string
  message: string
  status: number
  details?: { field?: string; reason?: string } | null
  retryable?: boolean
  retryAfterSeconds?: number | null
  traceId?: string | null
}

export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly details?: { field?: string; reason?: string }
  readonly retryable: boolean
  readonly retryAfterSeconds?: number
  readonly traceId?: string

  constructor(init: ApiErrorInit) {
    super(init.message)
    this.name = 'ApiError'
    this.code = init.code
    this.status = init.status
    this.details = init.details ?? undefined
    this.retryable = init.retryable ?? init.status >= 500
    this.retryAfterSeconds = init.retryAfterSeconds ?? undefined
    this.traceId = init.traceId ?? undefined
  }

  get isNetwork(): boolean {
    return this.status === 0
  }
}

type Params = Record<string, string | number | boolean | undefined | null>

/** Cross-origin API origin on deploy (Vercel → Render, DEC-14); empty locally so
 * the Vite proxy and the test fetch mock keep seeing same-origin paths. Browsers
 * call Render directly rather than through a Vercel rewrite so per-IP rate limits
 * (DEC-13) key on the real client, not on Vercel's shared egress. */
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')

export function buildUrl(path: string, params: Params = {}): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const query = search.toString()
  const base = path.startsWith('/api') || path.startsWith('/healthz') ? path : `/api/v1${path}`
  const url = `${API_BASE_URL}${base}`
  return query ? `${url}?${query}` : url
}

function statusFallbackCode(status: number): string {
  if (status === 404) return 'REGION_NOT_FOUND'
  if (status === 422) return 'VALIDATION_ERROR'
  if (status === 429) return 'RATE_LIMITED'
  if (status === 503) return 'SERVICE_UNAVAILABLE'
  if (status === 504) return 'TIMEOUT'
  return status >= 500 ? 'INTERNAL_ERROR' : 'BAD_REQUEST'
}

async function readError(response: Response): Promise<ApiError> {
  let envelope: ErrorEnvelope | null = null
  try {
    envelope = (await response.json()) as ErrorEnvelope
  } catch {
    envelope = null
  }
  const header = Number(response.headers.get('Retry-After'))
  const retryAfter =
    envelope?.error?.retry_after_seconds ?? (Number.isFinite(header) ? header : null)
  const traceHeader = response.headers.get('X-Trace-Id')
  return new ApiError({
    code: envelope?.error?.code ?? statusFallbackCode(response.status),
    message: envelope?.error?.message ?? response.statusText ?? 'Request failed',
    status: response.status,
    details: envelope?.error?.details,
    retryable: envelope?.error?.retryable ?? (response.status >= 500 || response.status === 429),
    retryAfterSeconds: retryAfter,
    traceId: envelope?.trace_id ?? traceHeader,
  })
}

async function request<T>(
  path: string,
  init: {
    method: 'GET' | 'POST'
    params?: Params
    body?: unknown
    signal?: AbortSignal
    cache?: RequestCache
  },
): Promise<T> {
  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, DEFAULT_TIMEOUT_MS)
  const onAbort = () => controller.abort()
  const external = init.signal
  if (external) {
    if (external.aborted) controller.abort()
    else external.addEventListener('abort', onAbort, { once: true })
  }

  let response: Response
  try {
    response = await fetch(buildUrl(path, init.params), {
      method: init.method,
      headers: init.body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      signal: controller.signal,
      cache: init.cache,
    })
  } catch (cause) {
    if (timedOut) {
      throw new ApiError({
        code: 'TIMEOUT',
        status: 504,
        message: 'The request timed out — please try again',
        retryable: true,
      })
    }
    if (external?.aborted) throw cause // react-query cancellation, not a failure
    throw new ApiError({
      code: 'NETWORK_ERROR',
      status: 0,
      message:
        typeof navigator !== 'undefined' && navigator.onLine === false
          ? 'You appear to be offline — check your connection'
          : 'Could not reach the server',
      retryable: true,
    })
  } finally {
    clearTimeout(timer)
    external?.removeEventListener('abort', onAbort)
  }

  if (!response.ok) throw await readError(response)
  if (response.status === 204) return undefined as T
  const text = await response.text()
  if (!text) return undefined as T
  try {
    return JSON.parse(text) as T
  } catch {
    throw new ApiError({
      code: 'BAD_RESPONSE',
      status: response.status,
      message: 'Server returned a malformed response',
      retryable: true,
    })
  }
}

export function apiGet<T>(
  path: string,
  params: Params = {},
  signal?: AbortSignal,
  cache?: RequestCache,
): Promise<T> {
  return request<T>(path, { method: 'GET', params, signal, cache })
}

export function apiPost<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { method: 'POST', body, signal })
}

/** Best-effort FE error beacon (docs/02 §7.1) — drops silently on failure (S-15). */
export function reportClientError(payload: {
  message: string
  code?: string
  trace_id?: string | null
}): void {
  const body = {
    message: payload.message.slice(0, 500),
    code: (payload.code ?? 'CLIENT_ERROR').slice(0, 64),
    trace_id: payload.trace_id ?? null,
    url: typeof window === 'undefined' ? null : window.location.href.slice(0, 300),
  }
  void fetch(buildUrl('/client-errors'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).catch(() => undefined)
}
