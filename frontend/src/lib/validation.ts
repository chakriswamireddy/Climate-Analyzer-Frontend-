/** Client-side mirrors of V-01..V-09 (docs/02 §8) — UX only; the server
 * stays authoritative. Messages reuse the server wording so an inline error
 * and a 422 envelope read the same. */

export const MIN_YEAR = 1950
export const DEFAULT_START_YEAR = 1990
export const DEFAULT_END_YEAR = 2020
export const REGION_ID_RE = /^r_[0-9a-f]{16}$/
// eslint-disable-next-line no-control-regex -- control chars are exactly what V-08 rejects
const CONTROL_RE = /[\x00-\x1f\x7f]/

export interface FieldError {
  field: string
  reason: string
}

export function currentYear(): number {
  return new Date().getFullYear()
}

/** DEC-04/DEC-17: the partial current year is out of range by default. */
export function lastFullYear(): number {
  return currentYear() - 1
}

/** V-01 + V-02 for the year range. */
export function validateRange(startYear: number, endYear: number): FieldError[] {
  const errors: FieldError[] = []
  if (!Number.isInteger(startYear) || !Number.isInteger(endYear)) {
    errors.push({ field: 'start_year', reason: 'years must be whole numbers' })
    return errors
  }
  if (startYear > endYear) {
    errors.push({ field: 'start_year', reason: 'start_year must be <= end_year' })
  }
  if (startYear < MIN_YEAR) {
    errors.push({ field: 'start_year', reason: `start_year must be >= ${MIN_YEAR}` })
  }
  if (endYear > lastFullYear()) {
    errors.push({
      field: 'end_year',
      reason: `end_year must be <= ${lastFullYear()} (the current year is still incomplete)`,
    })
  }
  return errors
}

export interface Bbox {
  min_lat: number
  min_lon: number
  max_lat: number
  max_lon: number
}

/** V-06 + V-16 bbox geometry (area, span, order, antimeridian). */
export function validateBbox(bbox: Bbox): FieldError[] {
  const errors: FieldError[] = []
  const { min_lat: minLat, min_lon: minLon, max_lat: maxLat, max_lon: maxLon } = bbox
  const finite = [minLat, minLon, maxLat, maxLon].every((value) => Number.isFinite(value))
  if (!finite) {
    return [{ field: 'bbox', reason: 'all four coordinates are required numbers' }]
  }
  if (minLat < -90 || maxLat > 90 || minLon < -180 || maxLon > 180) {
    errors.push({ field: 'bbox', reason: 'coordinates must be within lat −90…90, lon −180…180' })
  }
  if (minLon > maxLon) {
    errors.push({
      field: 'bbox',
      reason: 'bbox crosses the antimeridian (min_lon > max_lon); not supported',
    })
  }
  if (minLat >= maxLat) errors.push({ field: 'bbox', reason: 'min_lat must be < max_lat' })
  if (minLon >= maxLon && minLon <= maxLon) {
    errors.push({ field: 'bbox', reason: 'min_lon must be < max_lon' })
  }
  const spanLat = maxLat - minLat
  const spanLon = maxLon - minLon
  if (spanLat > 10 || spanLon > 10) {
    errors.push({ field: 'bbox', reason: 'bbox span must be <= 10 degrees per side' })
  }
  if (spanLat > 0 && spanLon > 0 && spanLat * spanLon > 25) {
    errors.push({ field: 'bbox', reason: 'Box too large — reduce to about 250 km per side' })
  }
  return errors
}

/** V-07 point bounds. */
export function validatePoint(lat: number, lon: number): FieldError[] {
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
    return [{ field: 'point', reason: 'latitude and longitude are required numbers' }]
  }
  if (lat < -90 || lat > 90) {
    return [{ field: 'point', reason: 'latitude must be between −90 and 90' }]
  }
  if (lon < -180 || lon > 180) {
    return [{ field: 'point', reason: 'longitude must be between −180 and 180' }]
  }
  return []
}

/** V-08 name rules. */
export function validateName(name: string): FieldError[] {
  if (name.length > 80) return [{ field: 'name', reason: 'name must be at most 80 characters' }]
  if (CONTROL_RE.test(name)) {
    return [{ field: 'name', reason: 'name must not contain control characters' }]
  }
  return []
}

/** V-09 search query rules (server rejects <2 or >80 too). */
export function isValidSearchQuery(q: string): boolean {
  const trimmed = q.trim()
  return trimmed.length >= 2 && trimmed.length <= 80 && !CONTROL_RE.test(trimmed)
}

export function formatFieldErrors(errors: FieldError[]): string[] {
  return errors.map((error) => `${error.field}: ${error.reason}`)
}
