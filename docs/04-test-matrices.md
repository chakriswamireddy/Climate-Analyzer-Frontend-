# 04 — Test Matrices

**Normative.** ID conventions and requirement/decision context from [`01-…decisions.md`](./01-requirements-analysis-and-decisions.md); design refs from [`02-…api-contracts.md`](./02-system-architecture-and-api-contracts.md); flows/edge cases from [`03-…failure-handling.md`](./03-flows-edge-cases-and-failure-handling.md). Every test lists expected results so it can be implemented without re-reading the brief.

---

## 1. Test Strategy

| Layer | Tooling | Scope | Gate |
|---|---|---|---|
| Unit (BE) | pytest + hypothesis (property tests for detector) | pure hazard/trend/validation functions | CI required |
| Integration (BE) | pytest-asyncio + `mongomock`-free **real Mongo** (testcontainers or CI service) + respx (mock HTTP for provider) | routers, services, ingest idempotency, rate limiter | CI required |
| Component (FE) | Vitest + React Testing Library | selector defaults, state branches (loading/empty/error) | CI required |
| E2E | Playwright (optional pre-submit; API-driven smoke in CI) | CF-01..05, CF-07 happy/sad paths | CI smoke (API-level) required; browser E2E best-effort |
| Non-functional | locust or k6 script (manual) | TC-N-01..05 | manual pre-submit |
| Manual | checklist §5 | demo polish, honesty review | pre-submit |

**Fixtures:** synthetic daily series generator (`tests/fixtures/series_factory.py`) producing known heatwave patterns (isolated 3-day streak, 2-day near-miss, gap-split streak, year-crossing streak, outlier spike, all-missing year).

**CI smoke (API-level), implemented P5:** the backend CI job (`.github/workflows/ci.yml`) seeds a deterministic ready region offline (`backend/scripts/seed_smoke_region.py` — direct stats insert, no provider, immune to EC-31) and runs `scripts/api-smoke.mjs` (18 checks: healthz/readyz, hazard registry, presets, region read + 404 envelope, summary/timeseries/events with NaN guard + ETag 304 + rate-limit headers, 422 envelopes, beacon 202/422). Browser E2E stays best-effort: `npm run test:e2e` (Playwright, TC-E-01..07 + CF-05 poll UX).

---

## 2. Unit Test Matrix (Backend, pure logic)

| ID | Case | Input | Expected result |
|---|---|---|---|
| TC-U-01 | Threshold percentile correctness | 30-year synthetic normals, known distribution | `thr[d]` equals `sorted(samples)[ceil(0.95n)−1]` ±0 (exact) |
| TC-U-02 | Seasonal conditioning | Summer-hot, winter-cold series | winter thresholds << summer thresholds; no winter day qualifies via summer threshold |
| TC-U-03 | Baseline fallback ladder | Series with only 8 valid baseline years | `wide_window` then `month` flags; unresolved DOYs → `None`; detection skips those days |
| TC-U-04 | Minimal heatwave (exactly 3 days) | 3 consecutive days just above thr | 1 event, duration 3 |
| TC-U-05 | Near-miss (2 days) | 2 above, 1 below, 2 above | 0 events |
| TC-U-06 | Strictly-above semantics | tmax == thr exactly | day does **not** qualify ("above") |
| TC-U-07 | Gap breaks streak | 3 above, 1 missing, 3 above | 2 events (documented DEC-20), not 1 |
| TC-U-08 | Outlier handling | tmax = 999 | flagged outlier; excluded; streak breaks; counters increment |
| TC-U-09 | Unit assertion | adapter receives °F-tagged payload (simulated NOAA) | conversion applied; unit-mismatch payload (unknown unit) → fatal |
| TC-U-10 | Coverage computation | year with 36 missing days | `coverage_pct ≈ 90.1`; trend exclusion if <80 |
| TC-U-11 | Bounds validation | param matrix (V-01..V-16 samples) | each violation raises matching field error; valid edge values pass |
| TC-U-12 | Year-boundary event | streak 12-28→01-03 | 1 event, `year`=start year; days split 4/3 (EC-15/CF-15) |
| TC-U-13 | Rolling mean window/edges | 10-year series | 5y window values correct; first 2 points `null` (min_periods=3) |
| TC-U-14 | % change happy | first3 mean 5, last3 mean 7 | `pct=40.0` |
| TC-U-15 | % change zero baseline | first3 mean 0, last3 mean 4 | `pct=None`, `headline_kind=new_activity`; no ∞ |
| TC-U-16 | Range-too-short gate | 5-year range | `range_too_short`, no % |
| TC-U-17 | Significance damping | synthetic weak trend p=0.4 | headline includes "not statistically significant"; strong trend p=0.01 → plain sentence |
| TC-U-18 | Idempotent region id | same spec, shuffled key order, float noise (17.3850 vs 17.385) | identical `_id` (canonicalization) |
| TC-U-19 | OLS vs known values | y = 2x+1, x=1..10 | slope≈2, r≈1, p≈0; MK (bonus) reports τ=1, monotonic |
| TC-U-20 | Feb 29 handling | leap-year series | no exception; Feb 29 assessed via folded window |
| TC-U-21 | Detector determinism (property test) | random series | same input → same events; events non-overlapping; durations ≥3 |
| TC-U-22 | Headline grammar | pct=40.0, region "Hyderabad", start 1990 | exact sentence matches FR-07 template |
| TC-U-23 | Suppressed low coverage | all years coverage <80 | `suppressed_low_coverage`; no trend stats emitted |
| TC-U-24 | Rounding | 39.6 → headline "40%"; stats 39.6 | headline rounds to whole; stats 1 decimal (EC-28) |
| TC-U-25 | Rate limiter bucket | 61 requests in a minute (fake clock) | first 60 pass; 61st 429 with Retry-After |
| TC-U-26 | Weighted quota pacing | 3 decade chunks with minute budget 50 (fake clock) + weight formula | 2 chunks sent immediately, 3rd waits for the next window; weight = `locations × max(1, (vars/10) × (days/30))` matches live calibration (1 loc/10 y/3 vars ≈ 36.5) |
| TC-U-27 | Provider 429 classification | 429 then 200 (window fits budget); 429 body naming an hourly limit; 429 whose wait exceeds the chunk budget | sleep-and-retry succeeds; hourly/daily → fail fast with `retry after HH:MM`; over-budget wait → retryable failure after exactly 1 attempt |
| TC-U-28 | If-None-Match weak comparison | strong `"x"`, weak `W/"x"`, `*`, mixed list, wrong tag, empty header, partial/unquoted tag | match only when the RFC 9110 opaque tags are equal after dropping `W/` and quotes (EC-34) |

---

## 3. Integration Test Matrix (API + Mongo + mocked provider)

| ID | Case | Setup | Expected result |
|---|---|---|---|
| TC-I-01 | POST /regions idempotency | same bbox twice (sequential) | 201 then 200; same `region_id`; 1 doc |
| TC-I-02 | Concurrent region create | 5 parallel identical POSTs | exactly 1 region doc; ≤1 running run; all responses share region_id |
| TC-I-03 | /hazards default | GET | heatwave present with `default:true` |
| TC-I-04 | Ingest happy path | mocked provider 3 years | daily rows, events, annual stats written; region `ready`; `data_version` bumped; counts in ingest_run |
| TC-I-05 | Provider 5xx then success | respx: 2×500 then 200 | retries observed (2 backoffs); success; attempts=3 recorded |
| TC-I-06 | Provider fatal schema drift | payload missing `temperature_2m_max` | run `failed_fatal`; region `failed`; **no** partial daily rows (tx abort) |
| TC-I-07 | Single-flight under storm | 10 parallel cold analysis GETs | 1 provider-fetch run; others 409-poll or join; provider called once per year |
| TC-I-08 | Analysis reads DB not provider | GET analysis with provider mock set to **fail** | 200 (data precomputed); zero outbound calls |
| TC-I-09 | Validation matrix | table-driven invalid params (V-01..V-16) | 422 with correct `details.field`; valid edge cases 200 |
| TC-I-10 | ETag 304 | GET twice with If-None-Match | second → 304, empty body |
| TC-I-11 | Rate limit headers/429 | 61st request | 429 envelope + `Retry-After` + X-RateLimit-* |
| TC-I-12 | Admin auth | wrong/missing/right token | 401 / 401 / 200; timing-safe compare (behavioral: no token echo) |
| TC-I-13 | data_version consistency | ingest bump between GETs | ETag changes; meta.data_version increments; L2 purged |
| TC-I-14 | DB down | point URI at closed port mid-suite | `/readyz` 503; analysis 503 retryable envelope; no stack leak |
| TC-I-15 | Events pagination | 84 events, page_size 25 | pages 1–4; totals 84; sort stable; `min_duration` filter narrows total |
| TC-I-16 | On-demand bounded wait | missing 1 year, provider slow (5 s) | within 8 s: 200 after fetch; with provider slow 30 s: 504 retryable + run continues; region eventually ready |
| TC-I-17 | Geocode proxy caching | 2 identical searches | 1 upstream call; second from L2 (respx call count) |
| TC-I-18 | Sweep orphaned run | run stuck `running` older than 10 min | sweeper → `failed_retryable`; re-enqueued |
| TC-I-19 | Partial coverage response | provider gap year in range | 200 with caveat + `coverage_pct` <100; charts data flagged; TC-E-06 consumes |
| TC-I-20 | Algo bump recompute | bump `ALGO_VERSION` | old stats ignored (versioned keys); recompute produces new rows; API serves new algo_version |
| TC-I-21 | Single-flight loser inherits failure | 2 parallel cold GETs, provider fails, 0% of the range present | both → 504 `TIMEOUT` retryable + `retry_after_seconds: 5`; runs all `failed_retryable`; nothing degraded cached (EC-33) |
| TC-I-22 | Weak ETag echo (CDN re-encode) | same GET with `If-None-Match: W/"…"` — the form Cloudflare returns whenever it gzip-encodes our strong `"…"` | 304 + empty body (RFC 9110 §13.1.2 weak comparison); a weak tag for a *different* representation still → 200 (EC-34) |
| TC-I-23 | Ingest window past the archive horizon | on-demand window `2018-01-01 … Dec 31 of the current year` (clamped to today) and a fully future window `2030` | provider is called with `end_date = today`, never a future date; a fully future window makes **no** provider call, records a `success` run and writes nothing (EC-35) |

---

## 4. Component/Frontend Test Matrix

| ID | Case | Expected |
|---|---|---|
| TC-F-01 | Initial render | hazard heatwave preselected; range 1990–2020; region empty-state prompt |
| TC-F-02 | Range guard | end<start blocked inline; values outside 1950..cur−1 blocked |
| TC-F-03 | Loading skeleton → success | skeleton visible during mock delay; charts after resolve |
| TC-F-04 | Error card + retry | 500 mock → ErrorCard with trace_id; Retry refetches |
| TC-F-05 | Empty no-events | zero totals → EmptyState copy (CF-07) |
| TC-F-06 | Partial banner | caveats present → banner lists years |
| TC-F-07 | 429 countdown | Retry-After honored; countdown UI |
| TC-F-08 | Stale query abort | rapid range toggles → final charts match final params (no out-of-order) |
| TC-F-09 | Events pagination controls | page nav updates query; total displayed |
| TC-F-10 | Map picker (bonus) | drag box → bbox values populated → POST /regions payload correct |

---

## 5. E2E Matrix (Playwright / manual script)

| ID | Flow | Steps | Expected |
|---|---|---|---|
| TC-E-01 | Cold-start happy path | open app → pick Hyderabad → wait ready | headline + timeseries + events table; heatwave preselected; request order correct |
| TC-E-02 | Custom bbox | enter coords valid → analyze | grid caveat footer; charts render |
| TC-E-03 | Range change | 1990–2020 → 2000–2020 | refetch; ETag may 304/200; charts update; no duplicate region POST |
| TC-E-04 | Chart interaction | toggle metric events↔days | chart re-renders client-side; a11y text updates |
| TC-E-05 | No-event region | pick cold synthetic region | EmptyState (CF-07); zero chart |
| TC-E-06 | Low-coverage region | mocked gap | DataQuality panel; no trend claim (CF-12) |
| TC-E-07 | Invalid URL params | hand-craft `?start_year=2021&end_year=1990` | 422 surfaced with Reset; app not blank |
| TC-E-08 | Offline simulation | devtools offline mid-fetch | offline banner; recovers on reconnect |
| TC-E-09 | Provider outage UX | mock 503 on backfill | "preparing/failed" card; Retry works after restore |
| TC-E-10 | Map select (bonus) | draw box on Leaflet | region created; charts |
| TC-E-11 | Export (bonus) | click CSV | file downloads with series rows; PDF print view opens |
| TC-E-12 | Rate-limit UX | burst >60 | countdown banner; app recovers |

---

## 6. Non-Functional / Security / Performance Tests

| ID | Aspect | Method | Expected |
|---|---|---|---|
| TC-N-01 | Safe external access | respx asserts timeouts set; no outbound on invalid input; 429 honored | configured retries ≤3; Retry-After respected |
| TC-N-02 | Input safety | fuzz bbox/name/q params (60 s) | only 4xx; no 500s; no hangs |
| TC-N-03 | Read latency warm | k6: 20 VUs × 2 min on hot region | p95 < 300 ms; 0 errors |
| TC-N-04 | Rate-limit effectiveness | k6 burst 500 rps | excess 429; server stable; headers correct |
| TC-N-05 | Cold-path resilience | kill provider mock mid-backfill | retries; honest region failed/partial; API up |
| TC-N-06 | No secret leakage | grep responses/logs for token/URI | absent; Authorization redacted in logs |
| TC-N-07 | Transaction atomicity | inject failure after events insert | abort → no visible events/stats for window |
| TC-N-08 | Bundle sanity | FE build size | initial JS < 300 KB gz (charts lazy-ok) |

**Tooling note (P5):** k6/locust unavailable in this environment → TC-N-01..05 executed with dependency-free equivalents: TC-N-01 respx + `test_http_client_carries_configured_timeouts`; TC-N-02 `scripts/fuzz-inputs.mjs` (150k req/60 s); TC-N-03 `scripts/bench-read.mjs` (20 concurrent clients, dedicated uvicorn with raised `RATE_LIMIT_ANALYSIS_PER_MIN` per 02 §15); TC-N-04 `scripts/burst-limit.mjs`; TC-N-05 live EC-31 provider outage + 18 failure-path tests. Results table: 05 §P5. TC-N-06..08 are outside §9's `TC-N-01..05` criterion (tracked under the 05 security pass).

---

## 7. Edge-Case → Test Mapping

| EC | Tests |
|---|---|
| EC-01 gaps | TC-U-07, TC-I-19 |
| EC-02 duplicate POST | TC-I-01, TC-I-02 |
| EC-03 duplicate rows | TC-I-04 |
| EC-04 outliers | TC-U-08 |
| EC-05 units | TC-U-09, TC-I-06 |
| EC-06 leap/Feb29 | TC-U-20 |
| EC-07 short range | TC-U-16, TC-F-02 |
| EC-08 baseline gaps | TC-U-03 |
| EC-09 antimeridian/edges | TC-I-09 |
| EC-10 malformed region_id | TC-I-09 |
| EC-11 degraded grid | TC-I-19 (grid variant) |
| EC-12 provider partial | TC-I-16, TC-I-21, TC-E-09 |
| EC-13 zero baseline | TC-U-15 |
| EC-14 DST | TC-U-12 (date-only) |
| EC-15 current year | TC-I-09 |
| EC-16 ETag race | TC-I-10, TC-I-13 |
| EC-17 shared IP limits | TC-N-04, manual M-05 |
| EC-18 restart mid-backfill | TC-I-18 |
| EC-19 algo bump | TC-I-20 |
| EC-20 pagination drift | TC-I-15 |
| EC-21 mixed versions | TC-I-13, TC-F-08 |
| EC-22 geocode drift | TC-I-17 (failure variant) |
| EC-23 unimplemented hazard | TC-I-09 |
| EC-24 long event | TC-U-21 (property) |
| EC-25 zero-valid year | TC-U-10, TC-E-06 |
| EC-26 admin vs nightly | TC-I-18 (lock variant) |
| EC-27 oversized body | TC-I-09 |
| EC-28 rounding | TC-U-24 |
| EC-29 single-year chart | TC-F-03 |
| EC-30 no storage | TC-F-01 in private mode (manual) |
| EC-31 shared-IP provider quota | TC-U-26, TC-U-27 (+ P1 DoD CLI backfill against the live API) |
| EC-32 retry-budget exhaustion (P5 fix) | TC-I-18 sweeper variant (`test_sweeper_marks_exhausted_region_failed`, `test_sweeper_budget_resets_when_region_resubmitted`, `test_sweeper_leaves_live_run_alone`), TC-N-05 |
| EC-33 joined run failure serves degraded 200 (P5 fix) | TC-I-21 (`test_joined_run_failure_answers_504_not_degraded_200`), cf05 e2e (`frontend/e2e/cf05.spec.ts`) |
| EC-34 CDN downgrades strong ETag to weak `W/"…"` (P6 fix) | TC-I-22 (`test_etag_304_for_cdn_weak_form`), TC-U-28 (`tests/unit/test_middleware.py`) |
| EC-35 current-year window reaches past the archive horizon → fatal 400 on CF-05 (P6 fix) | TC-I-23 (`test_window_is_clamped_to_the_archive_horizon`, `test_fully_future_window_records_honest_noop_success`) |

---

## 8. Manual Review Checklist (pre-submit)

| ID | Check | Graded dimension |
|---|---|---|
| M-01 | Charts legible on laptop + mobile widths; colors colorblind-safe; axes labeled with units (°C, days, events) | NFR-04 |
| M-02 | README methodology matches DEC-03/07 exactly; "what I'd improve" lists grid-cell percentiles, Theil–Sen, multi-source | NFR-02/05, CONSTRAINT-02 |
| M-03 | README has setup, architecture diagram, data source, hazard logic, assumptions (01 §5), AI tools disclosure | DELIV-03/04, CONSTRAINT-02/05 |
| M-04 | Deployed URL works cold (accept Render cold-start); repo public; demo video optional link | DELIV-01/02 |
| M-05 | Rate limit + caching verifiable (headers via devtools); no console errors | NFR-06 |
| M-06 | Live code-change rehearsal: modify a constant (e.g., min_days) → tests still meaningful path understood | CONSTRAINT-05 |

---

## 9. Completion Criteria (per this file)

- All TC-U + TC-I green in CI with real Mongo.
- TC-F green in CI; TC-E-01..07 executed (Playwright or scripted manual) and recorded in PR description.
- TC-N-01..05 executed at least once pre-submit with results table in PR.
- New edge cases discovered during implementation are added to 03 §4 **and** mapped here before merge.
