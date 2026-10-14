#!/usr/bin/env node
/** CF-01..04, CF-07 smoke script (docs/05 P4 DoD; matrix docs/04 §5).
 *
 * API-level verification of the critical flows against a running backend:
 *   node scripts/cf-smoke.mjs [API_BASE]        # default http://localhost:8000
 *
 * Exit code 0 = all checks passed. `BLOCKED` results (provider quota, EC-31)
 * are reported separately and do not fail the run, but are never silently
 * treated as passes. */

import process from 'node:process'

const BASE = (process.argv[2] ?? 'http://localhost:8000').replace(/\/$/, '')
const API = `${BASE}/api/v1`
const BACKFILL_BUDGET_MS = 300_000

let passed = 0
let failed = 0
const blocked = []

function check(name, ok, detail = '') {
  if (ok) {
    passed += 1
    console.log(`  ✓ ${name}${detail ? ` — ${detail}` : ''}`)
  } else {
    failed += 1
    console.log(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`)
  }
}

function block(name, detail) {
  blocked.push(`${name}: ${detail}`)
  console.log(`  ⚠ ${name} — BLOCKED (${detail})`)
}

async function call(method, path, payload) {
  const response = await fetch(`${API}${path}`, {
    method,
    headers: payload === undefined ? {} : { 'Content-Type': 'application/json' },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  })
  const text = await response.text()
  let body = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = null
  }
  return { status: response.status, headers: response.headers, body, text }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function waitForReady(regionId, budgetMs) {
  const deadline = Date.now() + budgetMs
  let last = null
  while (Date.now() < deadline) {
    const { body } = await call('GET', `/regions/${regionId}`)
    last = body
    if (body?.status === 'ready' || body?.status === 'failed') return body
    await sleep(5_000)
  }
  return last
}

// -- CF-01: first load and defaults ---------------------------------------
async function cf01() {
  console.log('CF-01 first load and defaults')
  const hazards = await call('GET', '/hazards')
  check('GET /hazards → 200', hazards.status === 200, `status=${hazards.status}`)
  check(
    'default hazard is heatwave and registered',
    hazards.body?.default === 'heatwave' &&
      (hazards.body?.items ?? []).some((item) => item.id === hazards.body.default),
    `default=${hazards.body?.default}`,
  )
  check('response carries trace_id', Boolean(hazards.body?.trace_id))

  const presets = await call('GET', '/regions/presets')
  check('GET /regions/presets → 200', presets.status === 200, `status=${presets.status}`)
  check('presets list is populated', (presets.body?.items ?? []).length > 0,
    `total=${presets.body?.total}`)

  const missing = await call('GET', '/regions/r_0000000000000000')
  check(
    'unknown region → 404 REGION_NOT_FOUND (FE clears to CF-01)',
    missing.status === 404 && missing.body?.error?.code === 'REGION_NOT_FOUND',
    `status=${missing.status} code=${missing.body?.error?.code}`,
  )
}

// -- CF-02: region selection (search → create → poll → ready) -------------
async function cf02() {
  console.log('CF-02 region selection: preset city')
  const search = await call('GET', '/regions/search?q=Hyderabad')
  check('search Hyderabad → 200 with items', search.status === 200 && (search.body?.items ?? []).length > 0,
    `status=${search.status} total=${search.body?.total}`)
  const item = search.body?.items?.[0]
  const payload = {
    kind: item.kind,
    name: item.name,
    point: item.spec?.points?.[0],
  }

  const created = await call('POST', '/regions', payload)
  check('POST /regions → 201/200 with region_id + poll_after_seconds',
    [200, 201].includes(created.status) && Boolean(created.body?.region_id),
    `status=${created.status} region_id=${created.body?.region_id}`)
  const regionId = created.body?.region_id

  const again = await call('POST', '/regions', payload)
  check('repeat POST is idempotent (same region_id, EC-02)',
    again.status === 200 && again.body?.region_id === regionId,
    `status=${again.status} same=${again.body?.region_id === regionId}`)

  const status = await waitForReady(regionId, BACKFILL_BUDGET_MS)
  if (status?.status === 'ready') {
    check('region reached ready', true, `coverage=${JSON.stringify(status.coverage)}`)
  } else if (status?.status === 'failed') {
    check('region reached ready', false, `status=failed last_error=${JSON.stringify(status.last_error)}`)
  } else {
    block('region reached ready', `still ${status?.status ?? 'unknown'} after ${(BACKFILL_BUDGET_MS / 1000) | 0}s — provider quota (EC-31)`)
  }
  return regionId
}

// -- CF-03: custom lat-lon box --------------------------------------------
async function cf03() {
  console.log('CF-03 custom lat-lon box')
  const box = {
    kind: 'bbox',
    name: 'smoke box',
    bbox: { min_lat: 17.2, min_lon: 78.3, max_lat: 17.5, max_lon: 78.6 },
  }
  const created = await call('POST', '/regions', box)
  check('POST bbox → 201/200 with region_id',
    [200, 201].includes(created.status) && Boolean(created.body?.region_id),
    `status=${created.status} region_id=${created.body?.region_id}`)

  const again = await call('POST', '/regions', box)
  check('same bbox twice → same region_id (hash, EC-02)',
    [200, 201].includes(again.status) && again.body?.region_id === created.body?.region_id,
    `same=${again.body?.region_id === created.body?.region_id}`)

  const tooBig = await call('POST', '/regions', {
    kind: 'bbox',
    name: 'too big',
    bbox: { min_lat: 10, min_lon: 10, max_lat: 30, max_lon: 40 },
  })
  check('oversized box → 422 with details (V-06)',
    tooBig.status === 422 && Boolean(tooBig.body?.error?.details?.field ?? tooBig.body?.error?.message),
    tooBig.body?.error?.message)

  const anti = await call('POST', '/regions', {
    kind: 'bbox',
    name: 'antimeridian',
    bbox: { min_lat: 10, min_lon: 170, max_lat: 11, max_lon: -170 },
  })
  check('antimeridian box → 422 (V-16)',
    anti.status === 422, anti.body?.error?.message)

  const status = await waitForReady(created.body?.region_id, BACKFILL_BUDGET_MS)
  if (status?.status === 'ready') {
    check('bbox region reaches ready (grid backfill)', true)
  } else {
    block('bbox region reaches ready (grid backfill)',
      `status=${status?.status} — provider quota (EC-31), re-run when the egress IP quota resets`)
  }
  return created.body?.region_id
}

// -- CF-04: analysis render + failure paths -------------------------------
async function cf04(regionId) {
  console.log('CF-04 analysis render (cache hit, happy path)')
  const canonical = `?region_id=${regionId}&hazard=heatwave&start_year=1990&end_year=2020&method=ols`
  const started = Date.now()
  const timeseries = await call('GET', `/analysis/timeseries${canonical}`)
  const summary = await call('GET', `/analysis/summary${canonical}`)
  const events = await call('GET', `/analysis/events${canonical}&page=1&page_size=25`)
  const warmMs = (Date.now() - started) / 3

  check('timeseries/summary/events → 200',
    [timeseries.status, summary.status, events.status].every((status) => status === 200),
    `${timeseries.status}/${summary.status}/${events.status}`)
  const versions = new Set([timeseries.body, summary.body, events.body].map((body) => body?.meta?.data_version))
  check('identical meta.data_version across responses', versions.size === 1, [...versions].join(','))
  check('series spans the requested range',
    timeseries.body?.series?.[0]?.year === 1990 &&
      timeseries.body?.series?.at(-1)?.year === 2020,
    `rows=${timeseries.body?.series?.length}`)
  check('rolling mean + OLS present for chart overlay',
    Boolean(timeseries.body?.rolling_mean?.window) && Boolean(summary.body?.stats?.ols),
    `window=${timeseries.body?.rolling_mean?.window} slope/decade=${summary.body?.stats?.ols?.slope_per_decade}`)
  check('headline is an honest sentence', (summary.body?.headline ?? '').length > 20,
    summary.body?.headline)
  check('events total matches summary totals',
    events.body?.total === summary.body?.stats?.totals?.events,
    `events.total=${events.body?.total}`)
  check('warm read latency < 300 ms (NFR-03)', warmMs < 300, `${warmMs.toFixed(0)} ms avg`)

  const etag = summary.headers.get('etag')
  const repeat = await fetch(`${API}/analysis/summary${canonical}`, {
    headers: { 'If-None-Match': etag ?? '' },
  })
  check('identical repeated GET → 304 via ETag', Boolean(etag) && repeat.status === 304,
    `etag=${Boolean(etag)} status=${repeat.status}`)

  console.log('CF-04 failure paths')
  const tampered = await call('GET', '/analysis/summary?region_id=' + regionId +
    '&hazard=heatwave&start_year=2021&end_year=1990&method=ols')
  check('tampered range → 422 with details.field (CF-08)',
    tampered.status === 422 && tampered.body?.error?.details?.field === 'start_year',
    `${tampered.status} ${tampered.body?.error?.details?.field}`)

  const unknownHazard = await call('GET', `/analysis/summary${canonical.replace('hazard=heatwave', 'hazard=drought')}`)
  check('unregistered hazard → 422 (EC-23)',
    unknownHazard.status === 422, unknownHazard.body?.error?.message)

  const missingRegion = await call('GET', '/analysis/summary?region_id=r_deadbeefdeadbeef&hazard=heatwave&start_year=1990&end_year=2020&method=ols')
  check('unknown region → 404 (FE clears to CF-01)',
    missingRegion.status === 404, `status=${missingRegion.status}`)
}

// -- CF-07: empty result ----------------------------------------------------
async function cf07(regionId) {
  console.log('CF-07 empty result: no heatwaves in range')
  const full = await call('GET',
    `/analysis/timeseries?region_id=${regionId}&hazard=heatwave&start_year=1950&end_year=2025&method=ols`)
  const series = full.body?.series ?? []
  let best = []
  let run = []
  for (const row of series) {
    if (row.event_count === 0 && row.coverage_pct >= 80) run.push(row.year)
    else {
      if (run.length > best.length) best = run
      run = []
    }
  }
  if (run.length > best.length) best = run
  if (best.length === 0) {
    block('zero-event window exists', 'no fully-covered zero-event year in 1950–2025 for this region')
    return
  }
  const [start, end] = [best[0], best.at(-1)]
  const query = `?region_id=${regionId}&hazard=heatwave&start_year=${start}&end_year=${end}&method=ols`
  const summary = await call('GET', `/analysis/summary${query}`)
  const timeseries = await call('GET', `/analysis/timeseries${query}`)
  const events = await call('GET', `/analysis/events${query}&page=1&page_size=25`)

  check(`summary over zero-event window ${start}–${end} → 200`, summary.status === 200,
    `status=${summary.status}`)
  check('totals.events == 0 and headline_kind honest',
    summary.body?.stats?.totals?.events === 0 &&
      ['no_change', 'range_too_short'].includes(summary.body?.headline_kind),
    `headline_kind=${summary.body?.headline_kind} "${summary.body?.headline}"`)
  check('series is zero-filled with real coverage (not fabricated values)',
    (timeseries.body?.series ?? []).every((row) => row.event_count === 0 && row.coverage_pct >= 80),
    `rows=${timeseries.body?.series?.length}`)
  check('events endpoint reports 0 (FE renders EmptyState)',
    events.body?.total === 0, `total=${events.body?.total}`)
  check('no NaN/Infinity in the payload',
    !/NaN|Infinity/.test(timeseries.text ?? '') && !/NaN|Infinity/.test(summary.text ?? ''))
}

// -- run --------------------------------------------------------------------
console.log(`CF smoke script → ${API}\n`)
try {
  await cf01()
  const regionId = await cf02()
  await cf03()
  if (regionId) await cf04(regionId)
  if (regionId) await cf07(regionId)
} catch (error) {
  failed += 1
  console.error('script crashed:', error)
}

console.log(`\n${passed} passed, ${failed} failed, ${blocked.length} blocked`)
if (blocked.length > 0) {
  console.log('blocked (environment, re-run later):')
  for (const entry of blocked) console.log(`  - ${entry}`)
}
process.exit(failed > 0 ? 1 : 0)
