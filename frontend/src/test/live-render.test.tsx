import { beforeAll, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderApp } from './renderApp'

/** Live FE verification against a running backend (docs/05 P4 DoD).
 * Not part of `npm test` — run explicitly with `npm run test:live` after
 * `docker compose up` (and `node scripts/cf-smoke.mjs` first if the egress IP
 * is quota-limited). jsdom has no canvas, so only the chart component is
 * mocked; App, react-query, fetch and the API are all real. */

const API = 'http://localhost:8000/api/v1'

vi.mock('react-chartjs-2', async () => {
  const react = await import('react')
  return {
    Bar: (props: { data?: { labels?: string[]; datasets?: { data?: unknown[] }[] } }) =>
      react.createElement('div', {
        'data-testid': 'time-series-chart',
        'data-labels': (props.data?.labels ?? []).join(','),
        'data-values': (props.data?.datasets?.[0]?.data ?? []).join(','),
      }),
  }
})

// jsdom's AbortSignal is a different realm than undici's — drop it for live calls.
function stubLiveFetch() {
  const realFetch = globalThis.fetch.bind(globalThis)
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) =>
    realFetch(new URL(String(input), 'http://localhost:8000'), { ...init, signal: undefined }),
  )
}

let regionId = ''

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return (await response.json()) as T
}

beforeAll(async () => {
  stubLiveFetch()
  const search = await call<{
    items: { name: string; kind: string; spec: { points?: { lat: number; lon: number }[] } }[]
  }>('GET', '/regions/search?q=Hyderabad')
  const first = search.items[0]
  const created = await call<{ region_id: string; status: string }>('POST', '/regions', {
    kind: first.kind,
    name: first.name,
    point: first.spec.points?.[0],
  })
  regionId = created.region_id
  const deadline = Date.now() + 300_000
  let status = created.status
  while (status !== 'ready' && status !== 'failed' && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 5_000))
    const doc = await call<{ status: string }>('GET', `/regions/${regionId}`)
    status = doc.status
  }
  if (status !== 'ready') {
    throw new Error(
      `region ${regionId} is ${status} — run scripts/cf-smoke.mjs (provider quota, EC-31?)`,
    )
  }
}, 320_000)

it('renders the real app against the live API (CF-01 + CF-04)', async () => {
  stubLiveFetch()
  renderApp(`/?region_id=${regionId}&start_year=1990&end_year=2020`)
  const card = await screen.findByTestId('summary-card', {}, { timeout: 20_000 })
  expect(card).toHaveTextContent('Heatwaves have decreased')
  expect(screen.getByTestId('chart-summary').textContent).toContain('1990–2020')
  expect(await screen.findByText(/events, 1990–2020/)).toBeInTheDocument()
  expect(screen.getByTestId('methodology')).toBeInTheDocument()
  expect(screen.getByLabelText('Hazard')).toHaveValue('heatwave')
  expect(screen.getByLabelText('Start year')).toHaveValue(1990)
})

it('renders the empty state against the live API (CF-07)', async () => {
  stubLiveFetch()
  renderApp(`/?region_id=${regionId}&start_year=2019&end_year=2020`)
  const empty = await screen.findByTestId('empty-state', {}, { timeout: 20_000 })
  expect(empty).toHaveTextContent('No heatwave events detected')
  expect(screen.getByTestId('time-series-chart')).toBeInTheDocument()
})

it('blocks tampered URL params before any request (CF-04 / CF-08)', async () => {
  stubLiveFetch()
  renderApp(`/?region_id=${regionId}&start_year=2021&end_year=1990`)
  const card = await screen.findByTestId('error-card')
  expect(card).toHaveTextContent('Invalid analysis parameters')
  await waitFor(() => expect(screen.queryByTestId('summary-card')).not.toBeInTheDocument())
})

it('clears an unknown region to the CF-01 copy (CF-04 failure path)', async () => {
  stubLiveFetch()
  // a never-created region id → 404 branch proves the FE clears to CF-01 copy
  renderApp('/?region_id=r_deadbeefdeadbeef&start_year=1990&end_year=2020')
  const card = await screen.findByTestId('error-card', {}, { timeout: 20_000 })
  expect(card).toHaveTextContent('Region not found')
})
