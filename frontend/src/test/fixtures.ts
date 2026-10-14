import type {
  ErrorEnvelope,
  EventsResponse,
  HazardItem,
  HazardsResponse,
  PresetsResponse,
  RegionStatusResponse,
  ResponseMeta,
  SeriesRow,
  SummaryResponse,
  TimeseriesResponse,
} from '../lib/types'

export const REGION_ID = 'r_0123456789abcdef'
export const TRACE_ID = 'trace-abc123'

export const META: ResponseMeta = {
  algo_version: 'p4-1',
  data_version: 'v1',
  generated_at: '2026-10-10T00:00:00Z',
  source: 'open-meteo archive',
  baseline: '1991-2020',
  cache: 'hit',
  caveats: [],
}

export const HAZARD: HazardItem = {
  id: 'heatwave',
  label: 'Heatwave',
  default: true,
  day_metric: 'heatwave_days',
  min_days: 3,
  threshold: 'regional 95th percentile of daily Tmax',
  description: '3+ consecutive days above the regional 95th-percentile threshold.',
}

export function hazardsFixture(): HazardsResponse {
  return { items: [HAZARD], default: 'heatwave', total: 1, trace_id: TRACE_ID }
}

export function presetsFixture(): PresetsResponse {
  return {
    items: [
      {
        id: 'p_delhi',
        kind: 'city',
        name: 'Delhi',
        spec: { mode: 'grid', points: [{ lat: 28.61, lon: 77.21 }] },
      },
      {
        id: 'p_kerala',
        kind: 'state',
        name: 'Kerala',
        spec: { mode: 'grid', points: [{ lat: 10.5, lon: 76.2 }] },
      },
    ],
    total: 2,
    trace_id: TRACE_ID,
  }
}

export function regionFixture(overrides: Partial<RegionStatusResponse> = {}): RegionStatusResponse {
  return {
    region_id: REGION_ID,
    name: 'Test City',
    kind: 'city',
    status: 'ready',
    data_version: 'v1',
    coverage: { start_year: 1950, end_year: 2025, years: 76 },
    created_at: '2026-10-10T00:00:00Z',
    ...overrides,
  }
}

export function makeSeries(startYear = 1990, endYear = 2020): SeriesRow[] {
  const rows: SeriesRow[] = []
  for (let year = startYear; year <= endYear; year += 1) {
    rows.push({
      year,
      event_count: 2,
      heatwave_days: 8,
      mean_duration: 4,
      max_duration: 6,
      mean_exceedance: 1.2,
      coverage_pct: 100,
    })
  }
  return rows
}

export function timeseriesFixture(overrides: Partial<TimeseriesResponse> = {}): TimeseriesResponse {
  const series = overrides.series ?? makeSeries()
  return {
    region: { region_id: REGION_ID, name: 'Test City', kind: 'city' },
    hazard: 'heatwave',
    range: { start_year: 1990, end_year: 2020, include_current_year: false },
    series,
    rolling_mean: {
      window: 5,
      align: 'trailing',
      metric: 'heatwave_days',
      values: series.map((row) => ({ year: row.year, value: 8 })),
    },
    meta: META,
    trace_id: TRACE_ID,
    ...overrides,
  }
}

export function summaryFixture(overrides: Partial<SummaryResponse> = {}): SummaryResponse {
  return {
    headline: 'Heatwaves up 42% over 1990–2020',
    headline_kind: 'pct_increase',
    stats: {
      pct_change_days: 42,
      pct_change_events: 31,
      baseline_window: { start_year: 1990, end_year: 1992, mean_days: 6 },
      recent_window: { start_year: 2018, end_year: 2020, mean_days: 8.5 },
      ols: {
        slope_per_year: 0.12,
        slope_per_decade: 1.2,
        intercept: -230.4,
        r: 0.7,
        p_value: 0.001,
        stderr: 0.03,
      },
      mann_kendall: { tau: 0.4, p_value: 0.01, s: 120, n: 31 },
      totals: { events: 60, days: 240, longest_event_days: 7 },
    },
    caveats: [],
    meta: META,
    trace_id: TRACE_ID,
    ...overrides,
  }
}

export function eventsFixture(page = 1, total = 60): EventsResponse {
  const pageSize = 25
  const items = Array.from({ length: pageSize }, (_, index) => ({
    event_id: `e-${page}-${index}`,
    start_date: '2010-06-10',
    end_date: '2010-06-14',
    year: 2010,
    duration_days: 5,
    mean_exceedance: 1.5,
    peak_exceedance: 2.4,
  }))
  return { items, page, page_size: pageSize, total, meta: META, trace_id: TRACE_ID }
}

export function errorEnvelope(
  code: string,
  message: string,
  options: {
    retryable?: boolean
    retryAfter?: number | null
    field?: string
    reason?: string
  } = {},
): ErrorEnvelope {
  return {
    error: {
      code,
      message,
      details:
        options.field === undefined
          ? null
          : { field: options.field, reason: options.reason ?? message },
      retryable: options.retryable ?? false,
      retry_after_seconds: options.retryAfter ?? null,
    },
    trace_id: TRACE_ID,
  }
}
