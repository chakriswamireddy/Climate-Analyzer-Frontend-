#!/usr/bin/env node
/** TC-N-03 warm read latency (docs/04 §6): N concurrent readers over a hot
 * region for a fixed duration; p95 must stay under 300 ms (NFR-03).
 *
 *   node scripts/bench-read.mjs [API_BASE] [duration_s] [concurrency]
 *
 * Defaults: http://localhost:8000 60 20. The docs matrix asks for k6
 * 20 VUs × 2 min; k6 is not available in this environment, so this is a
 * dependency-free equivalent (20 concurrent readers, default 60 s — the
 * shortened duration is recorded honestly in docs/05 P5).
 *
 * NOTE: run against an instance with RATE_LIMIT_ANALYSIS_PER_MIN raised
 * (docs/02 §15 tunable), otherwise the 60/min bucket itself dominates the
 * numbers. The rate-limit behaviour under default config is TC-N-04. */

import process from 'node:process'

const API = process.argv[2] ?? 'http://localhost:8000'
const DURATION_S = Number(process.argv[3] ?? 60)
const CONCURRENCY = Number(process.argv[4] ?? 20)
const REGION = process.env.BENCH_REGION_ID ?? 'r_4ff4af333ad67ec1'

// rotate a handful of canonical windows so we exercise real assembly, not
// one L2 entry (all windows end ≤ 2025, the server's max for closed years)
const URLS = []
for (let start = 1990; start <= 2005; start += 5) {
  URLS.push(
    `${API}/api/v1/analysis/summary?region_id=${REGION}&hazard=heatwave&start_year=${start}&end_year=${start + 20}&metric=heatwave_days`,
  )
}
URLS.push(
  `${API}/api/v1/analysis/timeseries?region_id=${REGION}&hazard=heatwave&start_year=1990&end_year=2020&method=ols`,
  `${API}/api/v1/regions/${REGION}`,
)

async function one(url) {
  const started = performance.now()
  try {
    const response = await fetch(url, { headers: { accept: 'application/json' } })
    const ms = performance.now() - started
    return { ms, status: response.status, body: await response.text() }
  } catch (error) {
    return { ms: performance.now() - started, status: 0, body: String(error) }
  }
}

function percentile(sorted, fraction) {
  return sorted[Math.min(sorted.length - 1, Math.floor(fraction * sorted.length))]
}

console.log(
  `TC-N-03 bench → ${API} | ${CONCURRENCY} readers × ${DURATION_S}s | region ${REGION}`,
)

// warm-up (untimed): fill L2 + connect pools
await Promise.all(Array.from({ length: CONCURRENCY }, () => one(URLS[0])))
await one(URLS[0])

const samples = []
const errors = []
const deadline = Date.now() + DURATION_S * 1000
let cursor = 0
let inflight = 0

await new Promise((resolve) => {
  const pump = () => {
    while (inflight < CONCURRENCY && Date.now() < deadline) {
      inflight += 1
      const url = URLS[cursor++ % URLS.length]
      one(url).then((result) => {
        inflight -= 1
        if (result.status >= 200 && result.status < 400) samples.push(result.ms)
        else errors.push({ status: result.status, body: result.body.slice(0, 200) })
        pump()
      })
    }
    if (inflight === 0 && Date.now() >= deadline) resolve()
  }
  pump()
})

samples.sort((a, b) => a - b)
const pct = (f) => percentile(samples, f).toFixed(1)
const p95 = percentile(samples, 0.95)
console.log(`requests ok: ${samples.length} | errors: ${errors.length}`)
if (errors.length) console.log('first errors:', JSON.stringify(errors.slice(0, 3)))
console.log(
  `latency ms — p50 ${pct(0.5)} | p90 ${pct(0.9)} | p95 ${pct(0.95)} | p99 ${pct(0.99)} | max ${samples.at(-1)?.toFixed(1)}`,
)

const failed = errors.length > 0 || samples.length === 0 || p95 >= 300
console.log(failed ? 'TC-N-03 FAIL' : 'TC-N-03 PASS (p95 < 300 ms, 0 errors)')
process.exit(failed ? 1 : 0)
