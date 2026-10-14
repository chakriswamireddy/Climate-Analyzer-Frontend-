/** Shareable analysis links: the SPA query string mirrors the API params
 * (docs/02 §7.4) — `?region_id=…&hazard=…&start_year=…&end_year=…`.
 *
 * Parsing is defensive: malformed values become field errors instead of a
 * blank screen (CF-08 / TC-E-07), and `Reset to defaults` rewrites the URL. */

import {
  DEFAULT_END_YEAR,
  DEFAULT_START_YEAR,
  REGION_ID_RE,
  lastFullYear,
  validateRange,
  type FieldError,
} from './validation'

export interface AnalysisParams {
  regionId: string | null
  hazard: string
  startYear: number
  endYear: number
}

export const DEFAULT_ANALYSIS_PARAMS: AnalysisParams = {
  regionId: null,
  hazard: 'heatwave',
  startYear: DEFAULT_START_YEAR,
  endYear: DEFAULT_END_YEAR,
}

/** Structural parse only (malformed numbers, bad region ids). Range rules are
 * derived continuously by {@link validateParams} so they cannot go stale. */
export function parseAnalysisParams(search: string): {
  params: AnalysisParams
  errors: FieldError[]
} {
  const query = new URLSearchParams(search)
  const params: AnalysisParams = { ...DEFAULT_ANALYSIS_PARAMS }
  const errors: FieldError[] = []

  const regionId = query.get('region_id')
  if (regionId) {
    if (REGION_ID_RE.test(regionId)) params.regionId = regionId
    else errors.push({ field: 'region_id', reason: 'region_id must look like r_ + 16 hex digits' })
  }

  const hazard = query.get('hazard')
  if (hazard) params.hazard = hazard // registry membership checked once /hazards loads

  for (const [raw, field, assign] of [
    [query.get('start_year'), 'start_year', (value: number) => (params.startYear = value)],
    [query.get('end_year'), 'end_year', (value: number) => (params.endYear = value)],
  ] as const) {
    if (raw === null) continue
    const value = Number(raw)
    if (/^-?\d+$/.test(raw) && Number.isInteger(value)) assign(value)
    else errors.push({ field, reason: `${field} must be a whole number` })
  }

  return { params, errors }
}

/** Canonical query string for the current params (always shareable). */
export function analysisSearch(params: AnalysisParams): string {
  const query = new URLSearchParams()
  if (params.regionId) query.set('region_id', params.regionId)
  query.set('hazard', params.hazard)
  query.set('start_year', String(params.startYear))
  query.set('end_year', String(params.endYear))
  return `?${query.toString()}`
}

/** `replaceState` keeps back-button history clean while links stay copyable. */
export function writeAnalysisParams(params: AnalysisParams): void {
  if (typeof window === 'undefined') return
  const url = `${window.location.pathname}${analysisSearch(params)}${window.location.hash}`
  window.history.replaceState(null, '', url)
}

/** Range-only validation used after any param commit. */
export function validateParams(params: AnalysisParams): FieldError[] {
  return validateRange(params.startYear, params.endYear)
}

export { DEFAULT_END_YEAR, DEFAULT_START_YEAR, lastFullYear }
