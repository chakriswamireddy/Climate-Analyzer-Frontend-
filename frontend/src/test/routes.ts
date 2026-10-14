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
  hazards?: MockResult | Responder
  presets?: MockResult | Responder
  region?: MockResult | Responder
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
      respond: (url) => resolve(options.hazards, { body: hazardsFixture() }, url),
    },
    {
      match: (url) => url.pathname === '/api/v1/regions/presets',
      respond: (url) => resolve(options.presets, { body: presetsFixture() }, url),
    },
    {
      match: (url) => url.pathname === `/api/v1/regions/${REGION_ID}`,
      respond: (url) => resolve(options.region, { body: regionFixture() }, url),
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
