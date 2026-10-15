import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetch } from './mockFetch'
import { standardRoutes } from './routes'
import { renderApp } from './renderApp'
import { REGION_ID, regionFixture } from './fixtures'
import { backfillPollMs, lastErrorMessage } from '../lib/backfill'
import type { RegionStatusResponse } from '../lib/types'

// Chart.js needs a real canvas; the backfilling branches never render it,
// but App imports the module — mock it like the other suites do.
vi.mock('react-chartjs-2', async () => {
  const react = await import('react')
  return { Bar: () => react.createElement('div', { 'data-testid': 'time-series-chart' }) }
})

const DEFAULT_NOTE = 'First-time analysis can take ~30–60 s'
const SLOW_NOTE = 'Still preparing'

function backfillingRoute(
  counter: { calls: number },
  overrides: Partial<RegionStatusResponse> = {},
) {
  return () => {
    counter.calls += 1
    return { body: regionFixture({ status: 'backfilling', ...overrides }) }
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('backfillPollMs (CF-02 step 5)', () => {
  const backfilling = { status: 'backfilling' as const }

  it('does not poll once the region is no longer backfilling', () => {
    expect(backfillPollMs({ status: 'ready' }, 1_000, 2_000)).toBe(false)
    expect(backfillPollMs(undefined, 1_000, 2_000)).toBe(false)
  })

  it('polls at the server-suggested cadence inside the 2-minute window', () => {
    expect(backfillPollMs(backfilling, 1_000, 2_000)).toBe(5_000)
    expect(backfillPollMs({ ...backfilling, poll_after_seconds: 10 }, 1_000, 2_000)).toBe(10_000)
  })

  it('never keys the budget on created_at — first observation always polls fast', () => {
    // observedAt null (first observation after mount/reload) regardless of age
    expect(backfillPollMs(backfilling, null, 1_792_000_000_000)).toBe(5_000)
  })

  it('degrades to the slow interval after the window instead of stopping', () => {
    const observed = 1_000_000
    expect(backfillPollMs(backfilling, observed, observed + 119_999)).toBe(5_000)
    expect(backfillPollMs(backfilling, observed, observed + 120_000)).toBe(15_000)
    expect(backfillPollMs(backfilling, observed, observed + 3_600_000)).toBe(15_000)
  })
})

describe('lastErrorMessage', () => {
  it('extracts the API error envelope message', () => {
    expect(lastErrorMessage({ code: 'PROVIDER_HTTP_ERROR', message: 'provider 503' })).toBe(
      'provider 503',
    )
  })

  it('tolerates missing/blank/foreign shapes', () => {
    expect(lastErrorMessage(null)).toBeNull()
    expect(lastErrorMessage(undefined)).toBeNull()
    expect(lastErrorMessage('')).toBeNull()
    expect(lastErrorMessage({ code: 'X' })).toBeNull()
    expect(lastErrorMessage('boom')).toBe('boom')
  })
})

describe('preparing card while the backfill runs (CF-02 step 5)', () => {
  it('keeps polling a backfilling region whose created_at is hours old', async () => {
    const counter = { calls: 0 }
    installFetch(
      standardRoutes({
        region: backfillingRoute(counter, {
          created_at: new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString(),
        }),
      }),
    )
    renderApp(`/?region_id=${REGION_ID}`)

    const card = await screen.findByTestId('preparing-card')
    expect(card).toHaveTextContent(DEFAULT_NOTE)

    // Regression: the old poller keyed on created_at and stopped dead after
    // 120 s of region age — an aged region got exactly one fetch, ever.
    await waitFor(() => expect(counter.calls).toBeGreaterThanOrEqual(2), { timeout: 9_000 })
  }, 15_000)

  it('switches to the still-preparing copy after 2 min; Check again restarts fast polling', async () => {
    const user = userEvent.setup()
    let clock = 1_792_000_000_000
    vi.spyOn(Date, 'now').mockImplementation(() => clock)

    const counter = { calls: 0 }
    installFetch(standardRoutes({ region: backfillingRoute(counter) }))
    renderApp(`/?region_id=${REGION_ID}`)

    const card = await screen.findByTestId('preparing-card')
    expect(card).toHaveTextContent(DEFAULT_NOTE)

    // Cross the fast window; the next 5 s poll re-evaluates the interval.
    clock += 121_000
    await waitFor(() => expect(screen.getByTestId('preparing-card')).toHaveTextContent(SLOW_NOTE), {
      timeout: 9_000,
    })

    // Check again restarts the fast window from now: default copy + a fetch.
    const before = counter.calls
    await user.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(counter.calls).toBeGreaterThan(before))
    expect(screen.getByTestId('preparing-card')).toHaveTextContent(DEFAULT_NOTE)
    expect(screen.queryByTestId('preparing-last-error')).not.toBeInTheDocument()
  }, 15_000)

  it('surfaces last_error while the backfill is still running', async () => {
    installFetch(
      standardRoutes({
        region: {
          body: regionFixture({
            status: 'backfilling',
            last_error: {
              code: 'PROVIDER_HTTP_ERROR',
              message: 'provider returned 503 after 4 attempts',
            },
          }),
        },
      }),
    )
    renderApp(`/?region_id=${REGION_ID}`)

    await screen.findByTestId('preparing-card')
    const error = await screen.findByTestId('preparing-last-error')
    expect(error).toHaveTextContent('provider returned 503 after 4 attempts')
    expect(error).toHaveTextContent('retries automatically')
  })

  it('clears the preparing card the moment the region turns ready', async () => {
    const user = userEvent.setup()
    let ready = false
    installFetch(
      standardRoutes({
        region: () => ({ body: regionFixture(ready ? {} : { status: 'backfilling' }) }),
      }),
    )
    renderApp(`/?region_id=${REGION_ID}`)

    expect(await screen.findByTestId('preparing-card')).toBeInTheDocument()
    ready = true
    await user.click(screen.getByRole('button', { name: 'Check again' }))
    expect(await screen.findByTestId('summary-card')).toBeInTheDocument()
    expect(screen.queryByTestId('preparing-card')).not.toBeInTheDocument()
  })
})
