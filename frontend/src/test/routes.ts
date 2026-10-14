import {
  eventsFixture,
  hazardsFixture,
  presetsFixture,
  regionFixture,
  summaryFixture,
  timeseriesFixture,
  REGION_ID,
} from './fixtures'
import type { MockResult, Responder, Route } from './mockFetch'

export interface RouteOptions {
  hazards?: MockResult
  presets?: MockResult
  region?: MockResult
  timeseries?: MockResult | Responder
  summary?: MockResult | Responder
  events?: MockResult | Responder
}

function resolve(
  value: MockResult | Responder | undefined,
  fallback: MockResult,
  url: URL,
): MockResult | Promise<MockResult> {
  if (value === undefined) return fallback
  if (typeof value === 'function') return value(url)
  return value
}

export function standardRoutes(options: RouteOptions = {}): Route[] {
  return [
    {
      match: (url) => url.pathname === '/api/v1/hazards',
      respond: () => options.hazards ?? { body: hazardsFixture() },
    },
    {
      match: (url) => url.pathname === '/api/v1/regions/presets',
      respond: () => options.presets ?? { body: presetsFixture() },
    },
    {
      match: (url) => url.pathname === `/api/v1/regions/${REGION_ID}`,
      respond: () => options.region ?? { body: regionFixture() },
    },
    {
      match: (url) => url.pathname === '/api/v1/analysis/timeseries',
      respond: (url) => resolve(options.timeseries, { body: timeseriesFixture() }, url),
    },
    {
      match: (url) => url.pathname === '/api/v1/analysis/summary',
      respond: (url) => resolve(options.summary, { body: summaryFixture() }, url),
    },
    {
      match: (url) => url.pathname === '/api/v1/analysis/events',
      respond: (url) => resolve(options.events, { body: eventsFixture() }, url),
    },
  ]
}
