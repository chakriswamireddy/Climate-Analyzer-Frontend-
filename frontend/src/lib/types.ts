/** Response shapes of the API (docs/02 §7) — hand-maintained mirror. */

export type RegionKind = 'city' | 'state' | 'bbox'
export type RegionStatusValue = 'backfilling' | 'ready' | 'failed'

export interface HazardItem {
  id: string
  label: string
  default: boolean
  day_metric: string
  min_days: number
  threshold: string
  description: string
}

export interface HazardsResponse {
  items: HazardItem[]
  default: string
  total: number
  trace_id?: string
}

export interface RegionSpecShape {
  mode?: 'point' | 'grid'
  points?: { lat: number; lon: number }[]
  bbox?: number[]
}

export interface PresetItem {
  id: string
  kind: RegionKind
  name: string
  spec: RegionSpecShape
}

export interface PresetsResponse {
  items: PresetItem[]
  total: number
  trace_id?: string
}

export interface SearchItem {
  name: string
  admin1: string
  country: string
  country_code: string
  lat: number
  lon: number
  kind: RegionKind
  spec: RegionSpecShape
}

export interface SearchResponse {
  items: SearchItem[]
  total: number
  trace_id?: string
}

export interface RegionStatusResponse {
  region_id: string
  name: string
  kind: RegionKind
  status: RegionStatusValue
  data_version: string | null
  coverage: { start_year: number; end_year: number; years: number } | null
  last_error?: unknown
  created_at?: string
  updated_at?: string
  poll_after_seconds?: number
}

export interface RegionCreateResponse {
  region_id: string
  status: RegionStatusValue
  status_url: string
  poll_after_seconds?: number
  trace_id?: string
}

/** POST /regions body (docs/02 §7.3). */
export interface RegionCreateBody {
  preset_id?: string
  kind?: RegionKind
  name?: string
  point?: { lat: number; lon: number }
  bbox?: { min_lat: number; min_lon: number; max_lat: number; max_lon: number }
}

export interface SeriesRow {
  year: number
  event_count: number
  heatwave_days: number
  mean_duration: number
  max_duration: number
  mean_exceedance: number
  coverage_pct: number
}

export interface ResponseMeta {
  algo_version: string
  data_version: string
  generated_at: string
  source: string
  baseline: string
  cache: 'hit' | 'miss' | 'stale'
  caveats: string[]
}

export interface TimeseriesResponse {
  region: { region_id: string; name: string; kind: RegionKind }
  hazard: string
  range: { start_year: number; end_year: number; include_current_year: boolean }
  series: SeriesRow[]
  rolling_mean: {
    window: number
    align: string
    metric: string
    values: { year: number; value: number }[]
  }
  meta: ResponseMeta
  trace_id?: string
}

export interface OlsStats {
  slope_per_year: number
  slope_per_decade: number
  intercept: number | null
  r: number | null
  p_value: number | null
  stderr: number | null
}

export interface MannKendallStats {
  tau: number
  p_value: number | null
  s: number
  n: number
}

export interface WindowStat {
  start_year: number
  end_year: number
  mean_days: number
}

export interface SummaryStats {
  pct_change_days: number | null
  pct_change_events: number | null
  baseline_window: WindowStat | null
  recent_window: WindowStat | null
  ols: OlsStats | null
  mann_kendall: MannKendallStats | null
  totals: { events: number; days: number; longest_event_days: number }
}

export type HeadlineKind =
  | 'pct_increase'
  | 'pct_decrease'
  | 'no_change'
  | 'new_activity'
  | 'range_too_short'
  | 'suppressed_low_coverage'

export interface SummaryResponse {
  headline: string
  headline_kind: HeadlineKind
  stats: SummaryStats
  caveats: string[]
  meta: ResponseMeta
  trace_id?: string
}

export interface EventItem {
  event_id: string
  start_date: string
  end_date: string
  year: number
  duration_days: number
  mean_exceedance: number
  peak_exceedance: number
}

export interface EventsResponse {
  items: EventItem[]
  page: number
  page_size: number
  total: number
  meta: ResponseMeta
  trace_id?: string
}

export interface ErrorEnvelope {
  error: {
    code: string
    message: string
    details?: { field?: string; reason?: string } | null
    retryable: boolean
    retry_after_seconds: number | null
  }
  trace_id?: string
}
