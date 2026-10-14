#!/usr/bin/env node
/** CI API-level smoke (docs/04 §1 "CI smoke (API-level) required").
 *
 *   node scripts/api-smoke.mjs [API_BASE] [REGION_ID]
 *
 * Offline by design: reads registry/read-path endpoints + error envelopes
 * only — no region creation, no search, no provider dependency, so CI stays
 * deterministic under EC-31. Pair with `backend/scripts/seed_smoke_region.py`
 * which prints the region id this script expects (env REGION_ID overrides).
 * Exit 0 = all checks passed. */

import process from 'node:process'

const API = process.argv[2] ?? 'http://localhost:8000'
const REGION = process.argv[3] ?? process.env.REGION_ID ?? 'r_0000000000000001'
let passed = 0
let failed = 0

function check(name, ok, detail = '') {
  if (ok) {
    passed += 1
    console.log(`  ✓ ${name}`)
  } else {
    failed += 1
    console.log(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`)
  }
}

async function call(method, path, { headers = {}, ifNoneMatch, body } = {}) {
  const requestHeaders = { accept: 'application/json', ...headers }
  if (ifNoneMatch) requestHeaders['if-none-match'] = ifNoneMatch
  const response = await fetch(new URL(path, API), {
    method,
    headers: requestHeaders,
    body,
  })
  const text = await response.text()
  let parsed = null
  try {
    parsed = JSON.parse(text)
  } catch {
    parsed = null
  }
  return { status: response.status, text, body: parsed, headers: response.headers }
}

const hasEnvelope = (r, code) =>
  r.body?.error?.code === code && typeof r.body?.trace_id === 'string'

// -- health -----------------------------------------------------------------
console.log(`API smoke → ${API} | region ${REGION}`)
{
  const health = await call('GET', '/healthz')
  check('healthz 200 with trace_id',
    health.status === 200 && health.body?.status === 'ok' && health.body?.trace_id)
  const ready = await call('GET', '/readyz')
  check('readyz 200 (mongo + scheduler)', ready.status === 200 && ready.body?.status === 'ready',
    `status=${ready.status}`)
}

// -- registry ---------------------------------------------------------------
{
  const hazards = await call('GET', '/api/v1/hazards')
  const ids = (hazards.body?.items ?? []).map((item) => item.id)
  check('hazards registry: 200, default present, trace_id',
    hazards.status === 200 && ids.length > 0 && ids.includes(hazards.body?.default) &&
      hazards.body?.trace_id)
  const presets = await call('GET', '/api/v1/regions/presets')
  check('presets: 200 with items + trace_id',
    presets.status === 200 && (presets.body?.items?.length ?? 0) > 0 && presets.body?.trace_id)
}

// -- region read + 404 envelope --------------------------------------------
{
  const region = await call('GET', `/api/v1/regions/${REGION}`)
  check('seeded region: 200 ready with coverage',
    region.status === 200 && region.body?.status === 'ready' && region.body?.kind === 'city' &&
      region.body?.coverage?.years === 31,
    `status=${region.status}`)
  const missing = await call('GET', '/api/v1/regions/r_0000000000000000')
  check('unknown region: 404 REGION_NOT_FOUND envelope',
    missing.status === 404 && hasEnvelope(missing, 'REGION_NOT_FOUND'))
}

// -- analysis read path ------------------------------------------------------
const query = `region_id=${REGION}&hazard=heatwave&start_year=1990&end_year=2020`
{
  const summary = await call('GET', `/api/v1/analysis/summary?${query}&metric=heatwave_days`)
  check('summary: 200 headline + totals + meta + trace_id',
    summary.status === 200 && typeof summary.body?.headline === 'string' &&
      typeof summary.body?.stats?.totals?.events === 'number' &&
      Array.isArray(summary.body?.meta?.caveats) && typeof summary.body?.trace_id === 'string')
  check('summary: no NaN/Infinity in payload', !/NaN|Infinity/.test(summary.text))
  check('summary: x-ratelimit headers present',
    summary.headers.get('x-ratelimit-limit') === '60')

  const etag = summary.headers.get('etag')
  check('summary: ETag issued', Boolean(etag))
  if (etag) {
    const cached = await call('GET', `/api/v1/analysis/summary?${query}&metric=heatwave_days`,
      { ifNoneMatch: etag })
    check('repeat with If-None-Match → 304, empty body',
      cached.status === 304 && cached.text === '')
  }

  const series = await call('GET', `/api/v1/analysis/timeseries?${query}&method=ols`)
  check('timeseries: 200, 31 years, rolling mean, region kind',
    series.status === 200 && (series.body?.series?.length ?? 0) === 31 &&
      Array.isArray(series.body?.rolling_mean?.values) && series.body?.region?.kind === 'city')
  check('timeseries: no NaN/Infinity in payload', !/NaN|Infinity/.test(series.text))

  const events = await call('GET', `/api/v1/analysis/events?${query}&page=1&page_size=25`)
  check('events: 200 with items + pagination (31 total, 25/page)',
    events.status === 200 && events.body?.items?.length === 25 &&
      events.body?.total === 31 && events.body?.page === 1 && events.body?.page_size === 25)
}

// -- failure envelopes -------------------------------------------------------
{
  const inverted = await call('GET',
    `/api/v1/analysis/summary?region_id=${REGION}&hazard=heatwave&start_year=2021&end_year=1990`)
  check('inverted range: 422 envelope with field detail',
    inverted.status === 422 && hasEnvelope(inverted, 'VALIDATION_ERROR') &&
      inverted.body?.error?.details?.field === 'start_year',
    `status=${inverted.status} code=${inverted.body?.error?.code}`)

  const badHazard = await call('GET',
    `/api/v1/analysis/summary?region_id=${REGION}&hazard=meteor&start_year=1990&end_year=2020`)
  check('unknown hazard: 422 envelope',
    badHazard.status === 422 && badHazard.body?.error?.code === 'VALIDATION_ERROR',
    `status=${badHazard.status}`)

  const beacon = await call('POST', '/api/v1/client-errors', {
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ code: 'FE_CRASH', message: 'smoke', url: '/trends' }),
  })
  check('client-errors valid beacon: 202 accepted, no-store',
    beacon.status === 202 && beacon.body?.accepted === true &&
      beacon.headers.get('cache-control') === 'no-store',
    `status=${beacon.status}`)

  const beaconBad = await call('POST', '/api/v1/client-errors', {
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ code: 'c'.repeat(65) }),
  })
  check('client-errors oversized code: 422 envelope with field',
    beaconBad.status === 422 && beaconBad.body?.error?.details?.field === 'code',
    `status=${beaconBad.status}`)
}

console.log(`\n${passed} passed, ${failed} failed`)
process.exit(failed > 0 ? 1 : 0)
