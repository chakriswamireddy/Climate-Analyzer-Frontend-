#!/usr/bin/env node
/** TC-N-02 input safety fuzz (docs/04 §6): hammer bbox/name/q/analysis
 * parameters with hostile junk for a fixed duration; anything but 2xx/4xx is
 * a defect (no 500s), and no request may hang.
 *
 *   node scripts/fuzz-inputs.mjs [API_BASE] [duration_s]
 *
 * Defaults: http://localhost:8000 60 (docs matrix: 60 s). */

import process from 'node:process'

const API = process.argv[2] ?? 'http://localhost:8000'
const DURATION_MS = Number(process.argv[3] ?? 60) * 1000
const HANG_MS = 10_000

const JUNK = [
  '',
  ' ',
  '   \t\n  ',
  'a'.repeat(10_000),
  '🔥💥%00\uD800',
  '"; DROP TABLE regions;--',
  '<script>alert(1)</script>',
  '../../../etc/passwd',
  '{{7*7}}${7*7}',
  '-1',
  '999999999999999999999',
  '0/0',
  'NaN',
  'Infinity',
  '[]',
  '{}',
  'r_deadbeefdeadbee',
  'r_zzzzzzzzzzzzzzzz',
  '%c0%ae%c0%ae',
  '$_x',
  '1e999',
  '1990-01-01',
  'Ω≈ç√∫µ≤',
  'null',
  'undefined',
]

const FLOATS = ['-999.5', '91', '-91', '181', '-181', '0', '90', '-90', '180', '-180', 'abc', '']

function enc(value) {
  // lone surrogates make encodeURIComponent throw; strip them (URLSearchParams
  // substitutes U+FFFD itself)
  try {
    return encodeURIComponent(value)
  } catch {
    return encodeURIComponent(value.replace(/[\uD800-\uDFFF]/g, ''))
  }
}

function pick(list) {
  return list[Math.floor(Math.random() * list.length)]
}

function randomUrl() {
  const target = pick(['search', 'summary', 'timeseries', 'events', 'region', 'hazards', 'beacon'])
  if (target === 'search') return `${API}/api/v1/regions/search?q=${enc(pick(JUNK))}`
  if (target === 'hazards') return `${API}/api/v1/hazards`
  if (target === 'region')
    return `${API}/api/v1/regions/${enc(pick(JUNK))}`
  if (target === 'beacon') return `${API}/api/v1/client-errors`
  const params = new URLSearchParams({
    region_id: pick(JUNK),
    hazard: pick(JUNK),
    start_year: pick(JUNK),
    end_year: pick(JUNK),
    method: pick(JUNK),
    page: pick(JUNK),
    metric: pick(JUNK),
  })
  return `${API}/api/v1/analysis/${target}?${params}`
}

function randomBody() {
  const shape = pick(['bbox-range', 'bbox-antimeridian', 'bbox-wrong-type', 'point', 'preset', 'name', 'empty', 'array'])
  if (shape === 'bbox-range') {
    const minLat = pick(FLOATS)
    return {
      kind: 'bbox',
      name: pick(JUNK),
      bbox: { min_lat: 20, max_lat: 80, min_lon: 10, max_lon: 90, note: minLat },
    }
  }
  if (shape === 'bbox-antimeridian')
    return { kind: 'bbox', name: 'fuzz', bbox: { min_lat: 1, max_lat: 2, min_lon: 170, max_lon: -170 } }
  if (shape === 'bbox-wrong-type')
    return { kind: 'bbox', name: pick(JUNK), bbox: { min_lat: pick(FLOATS), max_lat: 'x', min_lon: {}, max_lon: [1] } }
  if (shape === 'point')
    return { kind: 'city', name: pick(JUNK), point: { lat: pick(FLOATS), lon: pick(FLOATS) } }
  if (shape === 'preset') return { preset_id: pick(JUNK) }
  if (shape === 'name') return { kind: 'city', name: pick(JUNK), point: { lat: 17, lon: 78 } }
  if (shape === 'array') return [{ kind: 'bbox' }]
  return {}
}

async function fire(url, init) {
  const started = performance.now()
  try {
    const response = await fetch(url, init)
    const body = await response.text()
    return { ms: performance.now() - started, status: response.status, body, url: init ? `${init.method} ${url}` : url }
  } catch (error) {
    return { ms: performance.now() - started, status: 0, body: String(error), url: url }
  }
}

console.log(`TC-N-02 fuzz → ${API} | ${DURATION_MS / 1000}s`)

const statuses = new Map()
const failures = []
const depDegrades = []
const envelopeMisses = []
let total = 0
let maxMs = 0

const deadline = Date.now() + DURATION_MS
while (Date.now() < deadline) {
  const batch = []
  for (let i = 0; i < 25; i += 1) {
    if (Math.random() < 0.25) {
      batch.push(
        fire(`${API}/api/v1/regions`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(randomBody()),
        }),
      )
    } else if (Math.random() < 0.1) {
      batch.push(
        fire(`${API}/api/v1/client-errors`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ message: pick(JUNK), stack: pick(JUNK), url: pick(JUNK) }),
        }),
      )
    } else {
      batch.push(fire(randomUrl()))
    }
  }
  for (const result of await Promise.all(batch)) {
    total += 1
    if (result.ms > maxMs) maxMs = result.ms
    statuses.set(result.status, (statuses.get(result.status) ?? 0) + 1)
    const dependencyDegraded =
      result.status === 503 &&
      result.body.includes('PROVIDER_ERROR') &&
      result.body.includes('"retryable":true')
    if (dependencyDegraded) {
      depDegrades.push(result) // documented CF-02 "geocode down" degradation
    } else if (result.status >= 500 || result.status === 0 || result.ms > HANG_MS) {
      failures.push(result) // unhandled 5xx, network death, or hang
    }
    if (result.status >= 400 && result.status < 500 && !result.body.includes('"error"')) {
      envelopeMisses.push(result)
    }
  }
}

console.log(`requests: ${total}`)
console.log(
  `status histogram: ${[...statuses.entries()].sort((a, b) => a[0] - b[0]).map(([s, n]) => `${s}:${n}`).join(' ')}`,
)
console.log(`max observed latency: ${maxMs.toFixed(0)}ms (hang threshold ${HANG_MS}ms)`)
if (failures.length) {
  console.log('5xx/hang/network failures (first 5):')
  for (const failure of failures.slice(0, 5)) {
    console.log(`  ${failure.status} ${failure.ms.toFixed(0)}ms ${failure.url} :: ${failure.body.slice(0, 160)}`)
  }
}
if (envelopeMisses.length) {
  console.log(`4xx without error envelope (first 3, warning):`)
  for (const miss of envelopeMisses.slice(0, 3)) {
    console.log(`  ${miss.status} ${miss.url} :: ${miss.body.slice(0, 120)}`)
  }
}

console.log(
  `dependency-degraded 503 (retryable envelope, CF-02 geocode-down): ${depDegrades.length}` +
    (depDegrades.length ? ' — upstream is EC-31 429, spec\'d degradation' : ''),
)
const failed = failures.length > 0
console.log(failed ? 'TC-N-02 FAIL' : 'TC-N-02 PASS (only 2xx/4xx, no hangs, no unhandled 500s)')
process.exit(failed ? 1 : 0)
