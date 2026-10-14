import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { installFetch, callsTo, sleep } from './mockFetch'
import { standardRoutes } from './routes'
import { renderApp } from './renderApp'
import {
  REGION_ID,
  TRACE_ID,
  eventsFixture,
  errorEnvelope,
  makeSeries,
  summaryFixture,
  timeseriesFixture,
} from './fixtures'
import type { SummaryResponse } from '../lib/types'

// Chart.js needs a real canvas; assertions target the mocked node's labels/values.
vi.mock('react-chartjs-2', async () => {
  const react = await import('react')
  interface BarProps {
    data?: { labels?: string[]; datasets?: { data?: unknown[] }[] }
  }
  return {
    Bar: (props: BarProps) =>
      react.createElement('div', {
        'data-testid': 'time-series-chart',
        'data-labels': (props.data?.labels ?? []).join(','),
        'data-values': (props.data?.datasets?.[0]?.data ?? []).join(','),
      }),
  }
})

const ANALYSIS_PATHS = [
  '/api/v1/analysis/timeseries',
  '/api/v1/analysis/summary',
  '/api/v1/analysis/events',
]

function analysisUrl(url: URL): string {
  return url.pathname
}

describe('frontend core (TC-F-01..09)', () => {
  it('TC-F-01: defaults come from the server registry', async () => {
    const { calls } = installFetch(standardRoutes())
    renderApp('/')

    expect(await screen.findByTestId('empty-prompt')).toBeInTheDocument()
    expect(screen.getByLabelText('Hazard')).toHaveValue('heatwave')
    expect(screen.getByLabelText('Start year')).toHaveValue(1990)
    expect(screen.getByLabelText('End year')).toHaveValue(2020)
    expect(screen.queryByTestId('analysis-skeleton')).not.toBeInTheDocument()
    expect(calls.filter((call) => ANALYSIS_PATHS.includes(analysisUrl(call.url)))).toHaveLength(0)
  })

  it('TC-F-02: invalid ranges are blocked inline and never fetched', async () => {
    const user = userEvent.setup()
    const { calls } = installFetch(standardRoutes())
    renderApp('/')
    await screen.findByTestId('empty-prompt')

    const end = screen.getByLabelText('End year')
    await user.clear(end)
    await user.type(end, '1989')
    expect(await screen.findByTestId('range-error')).toHaveTextContent(
      'start_year must be <= end_year',
    )
    expect(end).toHaveAttribute('aria-invalid', 'false')
    expect(screen.getByLabelText('Start year')).toHaveAttribute('aria-invalid', 'true')

    await user.clear(end)
    await user.type(end, '2100')
    expect(await screen.findByTestId('range-error')).toHaveTextContent('end_year must be <= ')

    expect(calls.filter((call) => ANALYSIS_PATHS.includes(analysisUrl(call.url)))).toHaveLength(0)
  })

  it('TC-F-03: skeleton shows during loading, charts after resolve', async () => {
    installFetch(
      standardRoutes({
        timeseries: { body: timeseriesFixture(), delayMs: 150 },
        summary: { body: summaryFixture(), delayMs: 150 },
        events: { body: eventsFixture(), delayMs: 150 },
      }),
    )
    renderApp(`/?region_id=${REGION_ID}`)

    expect(await screen.findByTestId('analysis-skeleton')).toBeInTheDocument()
    expect(await screen.findByTestId('summary-card')).toBeInTheDocument()
    expect(screen.getByTestId('chart-summary')).toBeInTheDocument()
    expect(screen.getByTestId('time-series-chart')).toBeInTheDocument()
    expect(screen.queryByTestId('analysis-skeleton')).not.toBeInTheDocument()
  })

  it('TC-F-04: 500 renders error card with trace id, Retry recovers', async () => {
    const user = userEvent.setup()
    let timeseriesCalls = 0
    installFetch(
      standardRoutes({
        timeseries: () => {
          timeseriesCalls += 1
          return timeseriesCalls === 1
            ? {
                status: 500,
                body: errorEnvelope('INTERNAL_ERROR', 'Analysis failed on the server', {
                  retryable: false,
                }),
              }
            : { body: timeseriesFixture() }
        },
      }),
    )
    renderApp(`/?region_id=${REGION_ID}`, { retry: false })

    const card = await screen.findByTestId('error-card')
    expect(card).toHaveTextContent('Analysis failed on the server')
    expect(screen.getByTestId('trace-id')).toHaveTextContent(TRACE_ID)
    expect(timeseriesCalls).toBe(1)

    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByTestId('summary-card')).toBeInTheDocument()
    expect(timeseriesCalls).toBe(2)
  })

  it('TC-F-05: zero events renders the honest empty state', async () => {
    const zeroStats: SummaryResponse = summaryFixture({
      headline: 'No heatwave events in 1990–2020',
      headline_kind: 'no_change',
    })
    zeroStats.stats.totals = { events: 0, days: 0, longest_event_days: 0 }
    installFetch(
      standardRoutes({
        summary: { body: zeroStats },
        events: { body: { ...eventsFixture(1, 0), items: [] } },
      }),
    )
    renderApp(`/?region_id=${REGION_ID}`)

    const empty = await screen.findByTestId('empty-state')
    expect(empty).toHaveTextContent('No heatwave events in 1990–2020')
    expect(empty).toHaveTextContent('An empty result is a result')
    expect(screen.getByTestId('time-series-chart')).toBeInTheDocument()
    expect(screen.queryByTestId('error-card')).not.toBeInTheDocument()
  })

  it('TC-F-06: caveats render the read-with-care banner', async () => {
    installFetch(
      standardRoutes({
        summary: {
          body: summaryFixture({
            caveats: ['2019 has only 62% daily coverage — treat that year with caution.'],
          }),
        },
      }),
    )
    renderApp(`/?region_id=${REGION_ID}`)

    const banner = await screen.findByTestId('caveat-banner')
    expect(banner).toHaveTextContent('Read with care')
    expect(banner).toHaveTextContent('2019 has only 62% daily coverage')
  })

  it('TC-F-07: 429 honors Retry-After, counts down, then recovers', async () => {
    let timeseriesCalls = 0
    installFetch(
      standardRoutes({
        timeseries: () => {
          timeseriesCalls += 1
          return timeseriesCalls === 1
            ? {
                status: 429,
                headers: { 'Retry-After': '1' },
                body: errorEnvelope('RATE_LIMITED', 'Too many requests', {
                  retryable: true,
                  retryAfter: 1,
                }),
              }
            : { body: timeseriesFixture() }
        },
      }),
    )
    renderApp(`/?region_id=${REGION_ID}`) // default retry policy

    const banner = await screen.findByTestId('rate-limit-banner')
    expect(banner).toHaveTextContent(/rate limit reached/i)
    expect(timeseriesCalls).toBe(1)

    expect(await screen.findByTestId('summary-card', {}, { timeout: 4000 })).toBeInTheDocument()
    expect(screen.queryByTestId('rate-limit-banner')).not.toBeInTheDocument()
    expect(timeseriesCalls).toBe(2)
  })

  it('TC-F-08: rapid range toggles abort stale requests, final data wins', async () => {
    const user = userEvent.setup()
    const { calls } = installFetch(
      standardRoutes({
        timeseries: (url) =>
          url.searchParams.get('end_year') === '1995'
            ? {
                delayMs: 300,
                body: timeseriesFixture({
                  series: makeSeries(1990, 1995),
                  range: { start_year: 1990, end_year: 1995, include_current_year: false },
                }),
              }
            : { body: timeseriesFixture() },
        summary: (url) =>
          url.searchParams.get('end_year') === '1995'
            ? { delayMs: 300, body: summaryFixture({ headline: 'STALE-1995' }) }
            : { body: summaryFixture() },
      }),
    )
    renderApp(`/?region_id=${REGION_ID}`)
    expect(await screen.findByTestId('summary-card')).toBeInTheDocument()

    const end = screen.getByLabelText('End year')
    await user.clear(end)
    await user.type(end, '1995') // slow, superseded before it resolves
    await user.clear(end)
    await user.type(end, '2020') // back to the fast, final range

    await sleep(450) // past the stale response window
    expect(screen.getByTestId('headline')).toHaveTextContent('Heatwaves up 42%')
    expect(screen.queryByText('STALE-1995')).not.toBeInTheDocument()
    const chart = screen.getByTestId('time-series-chart')
    expect(chart.getAttribute('data-labels')?.split(',').pop()).toBe('2020')

    const staleCall = calls.find(
      (call) =>
        call.url.pathname === '/api/v1/analysis/timeseries' &&
        call.url.searchParams.get('end_year') === '1995',
    )
    expect(staleCall).toBeDefined()
    expect(staleCall?.abortedAtSettle).toBe(true)
  })

  it('TC-F-09: events pagination updates the query and shows totals', async () => {
    const user = userEvent.setup()
    const { calls } = installFetch(
      standardRoutes({
        events: (url) => ({ body: eventsFixture(Number(url.searchParams.get('page') ?? 1)) }),
      }),
    )
    renderApp(`/?region_id=${REGION_ID}`)

    const pager = await screen.findByTestId('events-pagination')
    expect(pager).toHaveTextContent('Page 1 of 3 · 60 events')

    await user.click(screen.getByRole('button', { name: 'Next' }))
    await waitFor(() =>
      expect(screen.getByTestId('events-pagination')).toHaveTextContent('Page 2 of 3 · 60 events'),
    )

    const eventCalls = callsTo(calls, '/api/v1/analysis/events')
    expect(eventCalls.at(-1)?.url.searchParams.get('page')).toBe('2')
    expect(eventCalls.at(-1)?.url.searchParams.get('page_size')).toBe('25')
  })
})
