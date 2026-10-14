#!/usr/bin/env node
/** TC-N-04 rate-limit effectiveness (docs/04 §6): a burst far above the
 * configured bucket must shed the excess as 429 with correct headers while
 * the server stays healthy and recovers once the bucket refills.
 *
 *   node scripts/burst-limit.mjs [API_BASE] [burst] 
 *
 * Defaults: http://localhost:8000 500 (docs matrix: burst 500 rps).
 * Uses the default-config container — the analysis bucket is 60/min. */

import process from 'node:process'

const API = process.argv[2] ?? 'http://localhost:8000'
const BURST = Number(process.argv[3] ?? 500)
const URL = `${API}/api/v1/analysis/summary?region_id=r_4ff4af333ad67ec1&hazard=heatwave&start_year=1990&end_year=2020&metric=heatwave_days`

console.log(`TC-N-04 burst → ${API} | ${BURST} concurrent requests`)

const results = await Promise.all(
  Array.from({ length: BURST }, async () => {
    try {
      const response = await fetch(URL)
      const text = await response.text()
      return {
        status: response.status,
        retryAfter: response.headers.get('retry-after'),
        limit: response.headers.get('x-ratelimit-limit'),
        remaining: response.headers.get('x-ratelimit-remaining'),
        envelope: text.slice(0, 200),
      }
    } catch (error) {
      return { status: 0, error: String(error) }
    }
  }),
)

const counts = {}
for (const result of results) counts[result.status] = (counts[result.status] ?? 0) + 1
console.log('status histogram:', JSON.stringify(counts))

const limited = results.filter((result) => result.status === 429)
const ok = results.filter((result) => result.status >= 200 && result.status < 300)
const broken = results.filter((result) => result.status >= 500 || result.status === 0)

const headersOk = limited.every(
  (result) => result.retryAfter !== null && result.limit === '60' && result.remaining === '0',
)
const envelopeOk = limited.every((result) => result.envelope.includes('RATE_LIMITED'))

const health = await fetch(`${API}/healthz`)
const healthy = health.status === 200

console.log(
  `ok ${ok.length} | 429 ${limited.length} | 5xx/network ${broken.length} | ` +
    `429 headers ${headersOk ? 'ok' : 'BAD'} | envelope ${envelopeOk ? 'ok' : 'BAD'} | ` +
    `healthz after burst ${health.status}`,
)

const failed = limited.length === 0 || ok.length > 60 || broken.length > 0 ||
  !headersOk || !envelopeOk || !healthy
console.log(failed ? 'TC-N-04 FAIL' : 'TC-N-04 PASS (excess shed as 429, headers correct, server stable)')
process.exit(failed ? 1 : 0)
