import { vi } from 'vitest'

/** Hand-rolled fetch mock — jsdom ships no fetch/Response, and the api layer
 * only needs `ok`, `status`, `headers.get`, `json()` and `text()`. */

export interface MockResult {
  status?: number
  body?: unknown
  headers?: Record<string, string>
  delayMs?: number
}

export type Responder = (url: URL, init?: RequestInit) => MockResult | Promise<MockResult>

export interface Route {
  match: (url: URL) => boolean
  respond: Responder
}

export interface RecordedCall {
  url: URL
  init?: RequestInit
  /** set when the request was still aborted when the mock finished settling */
  abortedAtSettle: boolean
}

export interface FetchMock {
  fetchMock: ReturnType<typeof vi.fn>
  calls: RecordedCall[]
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function headerValue(headers: Record<string, string>, name: string): string | null {
  const wanted = name.toLowerCase()
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() === wanted) return value
  }
  return null
}

export function installFetch(routes: Route[]): FetchMock {
  const calls: RecordedCall[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://test.local')
    const call: RecordedCall = { url, init, abortedAtSettle: false }
    calls.push(call)
    const route = routes.find((candidate) => candidate.match(url))
    if (!route) throw new Error(`Unhandled fetch: ${url.pathname}${url.search}`)
    const result = await route.respond(url, init)
    if (result.delayMs) await sleep(result.delayMs)
    if (init?.signal?.aborted) {
      call.abortedAtSettle = true
      throw new DOMException('The operation was aborted.', 'AbortError')
    }
    const status = result.status ?? 200
    const text = result.body === undefined ? '' : JSON.stringify(result.body)
    const headers = result.headers ?? {}
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: status >= 400 ? 'Error' : 'OK',
      headers: { get: (name: string) => headerValue(headers, name) },
      json: async () => result.body,
      text: async () => text,
    }
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, calls }
}

export function callsTo(calls: RecordedCall[], pathname: string): RecordedCall[] {
  return calls.filter((call) => call.url.pathname === pathname)
}
