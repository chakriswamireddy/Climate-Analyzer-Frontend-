# 03 — End-to-End Flows, Edge Cases, and Failure Handling

**Normative.** IDs: CF = critical flow, EC = edge case. References: [`01-…decisions.md`](./01-requirements-analysis-and-decisions.md), [`02-…api-contracts.md`](./02-system-architecture-and-api-contracts.md). Test coverage for every CF/EC is in [`04-test-matrices.md`](./04-test-matrices.md).

---

## 1. End-to-End Overview (numbered)

1. **Boot:** FE SPA loads; BE starts, pings Mongo on `/readyz`; scheduler registers jobs (02 §14.1).
2. **First paint:** FE fetches `/hazards` + `/regions/presets`; defaults applied: hazard=`heatwave` (FR-03), range=1990–2020 (DEC-04), region=none (prompt shown).
3. **Region selection:** user picks preset, searches, types bbox, or (bonus) draws on map → `POST /regions` → region doc created/`backfilling` → FE polls `/regions/{id}`.
4. **Backfill:** thresholds computed (02 §5.1) → provider fetch with quality pipeline → transactional persist of daily/events/stats → `ready` (CF-05).
5. **Analysis:** FE react-query fetches `/analysis/timeseries` + `/analysis/summary` (+ `/analysis/events`) with canonical params → charts + headline render (CF-04).
6. **Interaction:** user changes hazard/range/region → refetch (in-flight dedupe + abort of stale queries); pagination on events; ETag 304s on unchanged data.
7. **Background steady state:** nightly incremental refresh bumps `data_version`; L2 purged; next FE fetch converges (EC-21).
8. **Failure paths:** any step may fail per CF-06..CF-14; FE states (loading/empty/error/partial) per §5 of this file.

---

## 2. Critical Flows

### CF-01 — First load and defaults

| | |
|---|---|
| **Preconditions** | BE healthy; FE deployed; no user state |
| **Steps** | 1. FE mounts `App`. 2. `useQuery(['/hazards'])` → 200 → hazard selector renders, `heatwave` preselected. 3. `useQuery(['/regions/presets'])` → dropdown populated. 4. Range picker initialized 1990–2020 (validated against 1950..cur−1). 5. No region → analysis panel shows empty state "Select a region to analyze" (not an error). |
| **Postconditions** | Interactive UI; no analysis data yet; no provider calls made |
| **Failure paths** | `/hazards` fails → retry ×2 (react-query) → fatal banner "Unable to load app configuration" + Reload button. `/regions/presets` fails → presets area shows "Browse unavailable — enter coordinates manually" (manual bbox path still works). |
| **Expected outcome** | Usable defaults within 2 s on warm CDN; zero rate-limit consumption beyond 2 GETs |

### CF-02 — Region selection: preset city

| | |
|---|---|
| **Preconditions** | CF-01 done |
| **Steps** | 1. User types "Hyderabad" in search (debounced 300 ms) → FE calls `/regions/search?q=` (rate class 10/min). 2. Results list (name, admin1, country). 3. User selects → FE builds spec `{kind:city, point}` → `POST /regions`. 4. BE: validate (V-07/V-08) → compute `_id=sha256(canonical)` → insert-if-absent → enqueue threshold+backfill job → `201 {region_id, status:backfilling, poll_after_seconds:5}`. 5. FE shows "Preparing data for Hyderabad…" with progress note; polls `/regions/{id}` every 5 s (max 24 polls = 2 min, then "still preparing — try again shortly" with manual retry). 6. Status `ready` → auto-trigger analysis (CF-04). |
| **Postconditions** | Region persisted; backfill complete or in progress; analysis rendered if ready |
| **Failure paths** | Search 429 → inline "Too many searches — wait N s". Search 503 (upstream geocode down) → message + manual bbox form stays enabled. POST /regions 422 → field errors under inputs. POST 429 → cooldown toast. Backfill fatal → status `failed` → FE error card "Could not load weather data for this region" + Retry (re-POST same spec). Poll timeout → non-blocking notice (user may leave/return; job continues server-side). |
| **Expected outcome** | Repeat selection of same city returns **200** (idempotent) with same region_id; no duplicate region docs (EC-02) |

### CF-03 — Custom lat-lon box (manual or map)

| | |
|---|---|
| **Preconditions** | CF-01 |
| **Steps** | 1a. Manual: user enters min/max lat/lon (FE mirrors V-06 bounds). 1b. Bonus: Leaflet map, drag rectangle → bbox readout. 2. FE validation passes → `POST /regions {kind:bbox, bbox}`. 3. BE validates (V-06, V-16 antimeridian rejection), builds 3×3 grid (DEC-10), same as CF-02 step 4+. |
| **Postconditions** | Region of kind `bbox` ready; series = areal mean (noted in UI footer "Based on a 3×3 grid average across the selected box") |
| **Failure paths** | Area > 25 deg² → 422 "Box too large — reduce to about 250 km per side" (FE prevents, BE enforces). Degraded grid (≥2 cells fail) → ingest still succeeds with `quality:degraded_grid` days; meta caveat surfaced (EC-11). Map tiles fail → map area shows error placeholder; manual form unaffected. |
| **Expected outcome** | Same bbox submitted twice → same `region_id` (hash), 200 on second POST |

### CF-04 — Analysis render (cache hit, happy path)

| | |
|---|---|
| **Preconditions** | Region `status: ready`; requested years within coverage |
| **Steps** | 1. FE builds canonical query `{region_id, hazard, start, end, method}` → react-query fires `/analysis/timeseries`, `/analysis/summary`, `/analysis/events?page=1`. 2. BE: validate → L2/ETag check → read `hazard_annual_stats` + compute rolling/OLS (small) → 200 with `meta.cache:hit|miss`. 3. FE: SummaryCard renders `headline` + `caveats`; TimeSeriesChart renders bars (`heatwave_days`, `event_count` toggles) + rolling-mean line + (bonus) OLS line; EventsTable renders page 1 with total. 4. Textual summary under charts for a11y + honesty ("Heatwave days per year, 1990–2020; 5-year rolling mean; OLS trend +0.42 days/decade, p=0.08"). |
| **Postconditions** | All three responses share identical `meta.data_version`; ETag stored for conditional refetch |
| **Failure paths** | 404 REGION_NOT_FOUND (stale FE cache) → clear region, back to CF-01 empty state. 409 REGION_NOT_READY → CF-05 wait path. 422 (user tampered URL params) → error card with field details + "Reset to defaults" action. 429 → countdown banner from `Retry-After`, auto-retry once. 5xx → ErrorCard with `trace_id` + Retry button. Partial coverage → CF-12 banner. |
| **Expected outcome** | p95 < 300 ms warm; identical repeated GETs → 304 via ETag (no body) |

### CF-05 — Cache-miss: on-demand backfill inside analysis

| | |
|---|---|
| **Preconditions** | Region exists but requested years not yet in `hazard_annual_stats` (user widened range after initial backfill, or nightly job pending) |
| **Steps** | 1. BE AnalysisService detects missing years ∩ coverage gap. 2. Single-flight: if no `running` ingest for region → start one (bounded by partial unique index, 02 §12). 3. If estimated work ≤ `ONDEMAND_WAIT_SECONDS` budget → request awaits (async), provider fetch → transactional persist → response assembled from fresh stats (200). 4. Else → `409 REGION_NOT_READY` + `retry_after_seconds: 5`; FE shows progress spinner, polls `/regions/{id}` + retries the GET with jittered backoff (3 tries) until ready. |
| **Postconditions** | Requested range fully covered OR attempts exhausted |
| **Failure paths** | Provider timeout mid-backfill → run `failed_retryable` (3 attempts) → final failure: analysis GET returns **200 with partial data + `meta.caveats` + missing years listed** OR `504 TIMEOUT` if nothing usable — rule: if ≥80% of requested years exist, return partial (honest, EC-12); else 504 retryable with "data preparation failed" card. Circuit open → immediate 503 `retryable` (no queue pile-up). |
| **Expected outcome** | No duplicate concurrent backfills under request storm (verified TC-I-07); user never waits > `ONDEMAND_WAIT_SECONDS` synchronously |

### CF-06 — External provider failure during backfill

| | |
|---|---|
| **Preconditions** | Backfill running |
| **Steps** | 1. Provider returns 5xx/timeout → httpx retry (1s, 2s, 4s + jitter; honor Retry-After on 429). 2. Success on retry → continue silently; attempts recorded on run. 3. Exhausted → run `failed_retryable`; sweeper re-enqueues up to 3 attempts. 4. Fatal (4xx, unit mismatch, >5% schema-invalid rows) → `failed_fatal`; region → `failed`; no partial garbage committed (transaction), thresholds for other regions unaffected. |
| **Postconditions** | Either data lands, or region honestly `failed`; no corrupt stats |
| **Failure paths / recovery** | User Retry button / nightly job re-attempts; admin `POST /admin/recompute` forces run; circuit breaker prevents hammering a downed provider (half-open probe 30 s) |
| **Expected outcome** | API process never crashes; provider outage degrades to "prepare data" UX, not 500 storms (TC-I-05) |

### CF-07 — Empty result: no heatwaves in range

| | |
|---|---|
| **Preconditions** | Region ready; coverage fine; detector finds zero events |
| **Steps** | 1. BE returns 200 with zero-filled series, `stats.totals.events=0`, `headline_kind: "no_change"`, headline: "No heatwave events detected in this region between 1990 and 2020." 2. FE renders EmptyState card with explanation ("Heatwave = ≥3 consecutive days above the regional 95th percentile…") + zero chart + link to methodology (README anchor). |
| **Postconditions** | User understands "no events" ≠ "broken app" |
| **Failure paths** | Zero events **because** coverage < 80% everywhere → `headline_kind: suppressed_low_coverage`, data-quality state instead of "no heatwaves" (honesty, NFR-05) — distinct FE state |
| **Expected outcome** | Never renders NaN/Infinity; charts render flat zero line (TC-E-05) |

### CF-08 — Invalid inputs

| | |
|---|---|
| **Steps** | 1. Any request violating V-01..V-16 → 422 envelope with `details.field`. 2. FE prevents most via form validation; URL-param tampering hits BE. 3. FE on 422: highlights offending control, shows message, does not clear previous good result (keeps last successful charts dimmed). |
| **Examples** | `start_year=2021&end_year=1990`; `start_year=1800`; `page_size=10000`; `region_id=../../etc`; bbox min>max; `hazard=drought` (not in registry → 422 listing supported hazards) |
| **Expected outcome** | Zero stack traces in responses; all rejections machine-coded (TC-I-09) |

### CF-09 — Rate-limited request

| | |
|---|---|
| **Steps** | 1. Bucket empty → 429 + `Retry-After` + `X-RateLimit-*` headers. 2. FE react-query: respects Retry-After, one automatic retry, then banner "Rate limit reached — retrying in Ns". Geocode box disabled during cooldown. |
| **Postconditions** | Server load bounded; honest UX |
| **Expected outcome** | Headers present on all rate-limited routes (TC-I-11) |

### CF-10 — Duplicate submissions / double-click

| | |
|---|---|
| **Scenarios** | (a) Double-click region create → FE disables submit while in-flight **and** BE idempotent by hash. (b) Retried POST after network timeout → same idempotency. (c) N users request same cold analysis simultaneously → single-flight backfill; all but originator get 409-poll or wait ≤ budget. (d) User re-sends identical analysis GET → ETag/304. |
| **Expected outcome** | Exactly one region doc and one `running` ingest per region under concurrency (TC-I-07, TC-U-18) |

### CF-11 — Conflicting updates: nightly ingest vs live reads

| | |
|---|---|
| **Steps** | 1. Nightly incremental commits `data_version: dv_12→dv_13`, purges L2 for region. 2. In-flight FE session still shows dv_12 data (fine). 3. Next user interaction refetches → new ETag → charts update; if `summary.meta.data_version ≠ timeseries.meta.data_version` mid-session (rare race across three parallel GETs), FE refetches summary once; persistent mismatch → caveat chip "Results refreshing…". 4. Threshold rebuild (algo bump) changes event definitions → same convergence; `meta.algo_version` surfaced in methodology tooltip. |
| **Expected outcome** | No torn reads (transactional commit); eventual consistency ≤ one refetch (TC-I-13) |

### CF-12 — Partial data coverage

| | |
|---|---|
| **Steps** | 1. Ingest stores gaps honestly (missing/outlier → unassessable days). 2. Annual rows with `coverage_pct < 80` are excluded from trend math (02 §5.4) but **still plotted** with a "low coverage" marker on the chart. 3. `meta.caveats` lists offending years; summary adds chip "Low data coverage in 1997". 4. If < 60% of requested years have coverage ≥ 80% → `headline_kind: suppressed_low_coverage`; FE shows data-quality panel, not a trend claim. |
| **Expected outcome** | Never fabricate; user always told what the data can/cannot support (TC-E-06) |

### CF-13 — Network interruption / client retry / abort

| | |
|---|---|
| **Steps** | 1. Changing filters aborts in-flight queries (AbortController via react-query `cancel`; sequence guard prevents out-of-order application). 2. Browser offline (`navigator.onLine` + fetch failure) → FE offline banner; react-query pauses + retries with backoff (cap 3) on reconnect. 3. Mid-analysis disconnect after BE commit → harmless (GET idempotent; backfill single-flight prevents duplicate work on refetch). 4. Poll loop interrupted → resumes on next interval; user can leave page, return later — server state is source of truth. |
| **Expected outcome** | No stuck spinners: every fetch has timeout (30 s FE) + error branch (TC-E-08) |

### CF-14 — Database unavailable

| | |
|---|---|
| **Steps** | 1. Mongo ping fails on `/readyz` → platform stops routing (Render health check); if mid-request: 503 `SERVICE_UNAVAILABLE` + `retry_after_seconds: 10`. 2. FE: ErrorCard "Service temporarily unavailable" + auto-retry (exponential, cap 3) + manual Retry. 3. On recovery: L2 empty → cache-miss path re-reads DB (stats durable) — no provider re-hit needed for already-covered years. |
| **Expected outcome** | Zero unhandled exceptions leak; recoverable without data loss (TC-I-14) |

### CF-15 — Event spanning year boundary

| | |
|---|---|
| **Steps** | 1. Streak 1999-12-28 → 2000-01-03 (7 days): one event, `year=1999` (start rule, DEC-21); `heatwave_days`: 4 in 1999, 3 in 2000. 2. Timeseries shows event_count(1999)+1, days split correctly; EventsTable shows single row spanning years. 3. Summary totals count the event once. |
| **Expected outcome** | No double counting; documented in methodology tooltip (TC-U-12) |

---

## 3. State Transitions (summary; full machines in 02 §14.2–14.4)

| Entity | States | Terminal? |
|---|---|---|
| Region | `backfilling → ready \| failed`; `failed → backfilling` (retry) | no |
| Ingest run | `created → running → success \| failed_retryable \| failed_fatal`; retryable→running (≤3) | success/fatal |
| FE analysis panel | `idle → loading → success \| empty \| error(retryable\|fatal) \| partial` | per interaction |
| Response freshness | `fresh → stale (L2 TTL) → revalidated` | continuous |

---

## 4. Edge-Case Matrix (EC)

| ID | Edge case | Handling (where) | Expected behavior |
|---|---|---|---|
| EC-01 | Missing days in provider data (gaps) | 02 §5.6; DEC-20 | Days stored `missing`; break streaks; coverage reduced; caveats if material |
| EC-02 | Duplicate POST /regions (double-click, retry) | 02 §12 idempotent hash id | 201 then 200; one doc |
| EC-03 | Duplicate rows from provider | unique `{region_id,date}` upsert | last-write-wins; counted in run stats |
| EC-04 | Outlier temps (sensor glitch, e.g. 999 °C, −999 fill values) | bounds check 02 §5.6 | flagged `outlier`, treated missing; never plotted |
| EC-05 | Unit mixup (future NOAA adapter, °F) | unit assertion, fatal mismatch | run `failed_fatal`; no silent °F-as-°C |
| EC-06 | Feb 29 / leap years | thresholds 1–365; Feb 29 folded | no crash; Feb 29 uses Feb-28-window threshold |
| EC-07 | Range shorter than 6 years | honesty gate 02 §5.4 | `range_too_short` headline; no % claim |
| EC-08 | Baseline (1991–2020) has heavy gaps | fallback ladder 02 §5.1 | wide-window → month → unresolved DOYs; meta lists unresolved count |
| EC-09 | Region at bbox edges / poles / antimeridian | V-06, V-16 | valid ranges accepted; antimeridian 422 explicit message |
| EC-10 | `region_id` malformed / injection strings | V-05 regex + 404 | 422/404; no regex DoS (anchored, fixed length) |
| EC-11 | Grid cell fetch failures (partial grid) | ≥5/9 cells rule; `degraded_grid` | areal mean of remaining; quality flag; caveat |
| EC-12 | Provider down for requested extension years | CF-06/CF-05 | ≥80% years exist → 200 partial + caveats; else 504 retryable |
| EC-13 | First-3-year mean = 0 in % formula | DEC-07 division guard | `new_activity` headline or `no_change`; never ∞/NaN |
| EC-14 | DST / timezone rollover on daily data | DEC-15 date-only strings | dates are calendar dates; no TZ arithmetic |
| EC-15 | Current partial year requested | DEC-17 | default excluded (422 if `end_year==cur` without flag); with flag → `partial_year: true` caveat |
| EC-16 | ETag race: body changes between hash and send | hash after full assembly in-memory | consistent 200; next GET gets new ETag |
| EC-17 | Rate limiter vs shared corporate IP | 429 UX; limits tunable via env | honest cooldown; documented limitation |
| EC-18 | Instance restart mid-backfill | sweeper 02 §14.3 | orphaned `running` → retryable; upserts make retry safe |
| EC-19 | Algo version bump mid-session | `algo_version` in meta + keys | cache misses naturally; FE shows "methodology updated" if version changes across refetch |
| EC-20 | Events `total` vs page drift during nightly recompute | stable sort `_id` tiebreak; total re-read per request | may shift by a few — acceptable; FE re-renders |
| EC-21 | Three parallel GETs return mixed `data_version` | FE cross-check 02 §13 | one summary refetch; caveat chip if persists |
| EC-22 | Geocode upstream shape change / malformed JSON | provider classify fatal; retry once for transient decode | 503 search path; manual entry still works |
| EC-23 | User selects hazard not yet implemented (future UI) | registry-driven selector | option absent (not disabled noise); BE 422 if forced |
| EC-24 | Very long event (e.g., 30-day monsoon stall pattern) | no max-duration cap | stored/plotted fully; duration chart y-axis auto |
| EC-25 | Zero valid days entire region-year (e.g., war data gap) | coverage 0 → excluded from trends | row plotted as gap (null), not 0; caveat year listed |
| EC-26 | Concurrent admin recompute + nightly job | same single-flight + run unique index | second waits/fails fast with 409-style message in admin response |
| EC-27 | JSON body > 10 KB on POST /regions | V-15 | 413-mapped BAD_REQUEST |
| EC-28 | Floating-point % noise (39.9999% → "40%") | rounding only at render (1 decimal) | headline rounds to whole %; stats keep 1 decimal |
| EC-29 | Chart with single year of data | FE guard | renders single point + note "trend needs ≥2 years" |
| EC-30 | Browser without localStorage / private mode | react-query memory cache fallback | app fully functional (no persistence required) |
| EC-31 | Shared-IP free-tier provider quota exhausted mid-backfill (weighted calls: 600/min, 5k/h, 10k/day per IP) | proactive weight pacing + 429 window handling (02 §5.3) | run `failed_retryable` with "retry after HH:MM"; no partial rows (§13); re-run resumes idempotently; a request never sleeps longer than its chunk budget |
| EC-32 | Provider stays down past `max_ingest_attempts` — 3 consecutive `failed_retryable` runs inside one backfilling episode (found in P4, fixed in P5) | episode-scoped sweeper budget, `scheduler.py::sweep_stale_runs` (02 §14.3/§14.2) | region → `failed` with `last_error.code = INGEST_RETRY_EXHAUSTED` + re-submit guidance; never stuck `backfilling` forever; a fresh `POST /regions` starts a new episode so old failures never starve a retry |
| EC-33 | Single-flight loser joins an on-demand run that then **fails** (provider down / quota) while the requested years are still <80% present (found in P4, fixed in P5) | `analysis.py::ensure_ready` adopts the joined run's recorded outcome (`_joined_failure`) instead of assuming success | both callers get `504 TIMEOUT` retryable per CF-05/EC-12 — never a `200` body assembled from the gap, which would freeze the FE on a `suppressed_low_coverage` panel while the data it needs already exists |
| EC-34 | CDN between client and origin re-encodes the response and downgrades our strong `"…"` ETag to the weak `W/"…"` (Cloudflare does this exactly when it gzip-encodes; brotli passes through) — found on the deployed URL in P6, where every gzip-only client that echoed its ETag back got `200` instead of `304` | `middleware.py::_matches` compares RFC 9110 opaque tags (strip `W/` and quotes) so `If-None-Match` uses the **weak** comparison the spec mandates | `W/"…"` for the same representation → `304` empty body; a weak tag of a *different* representation still → `200`; no body is ever re-sent for a matching tag |
| EC-35 | A current-year on-demand range builds a `Jan 1 … Dec 31` window; Open-Meteo rejects the future `end_date` with `400`, and a non-429 4xx is a `ProviderClientError` (fatal) — found on the live URL in P6, where CF-05 answered `502 PROVIDER_ERROR "unusable data"` instead of preparing | `ingest.py::run` clamps the window to **today** before fetching; a wholly future window records an honest no-op `success` run (no provider call, no writes) | the provider is only ever asked for `end_date ≤ today`; `ensure_ready` sees the years still missing and serves the EC-12 partial — never a fatal 502 for data that simply does not exist yet |
| EC-36 | An on-demand run fetches only its own window, and coverage was derived from that window — so a successful partial fetch **replaced** the region's stored extent (live: 1950–2025 collapsed to 2025–2026 the moment EC-35 let CF-05 through) | `DailyWeatherRepository.coverage_extent` recomputes coverage from every stored year (group on the `{region_id, year}` index) inside the same transaction as the write | coverage only ever grows: 2018–2020 followed by a 2021 fetch reports `2018–2021, 4 years`, and the full series stays visible in `/regions/{id}` |

---

## 5. Loading, Empty, and Error States (Frontend matrix)

| State | Trigger | UI |
|---|---|---|
| Loading (first analysis) | query in-flight, no cached data | Skeleton cards (chart-shaped), pulsing; controls disabled except Cancel |
| Loading (refetch) | param change with cached data | Keep old charts, top progress bar (stale-while-revalidate) |
| Preparing data | 409 REGION_NOT_READY / on-demand wait | "Preparing historical data… first-time analysis can take ~30–60 s" + poll indicator |
| Empty (no events) | 200, `totals.events == 0`, coverage OK | EmptyState card (CF-07) |
| Empty (no region) | initial | Prompt card with 3 selection methods (search / coordinates / map) |
| Data-quality suppressed | `suppressed_low_coverage` | DataQuality panel: coverage map of years, no trend claim |
| Partial | 200 + `meta.caveats` coverage entries | Amber banner listing years; charts marked |
| Error (4xx) | 404/422 | Message + field highlights / Reset action |
| Error (429) | rate limit | Countdown + auto-retry once |
| Error (5xx / network) | 500/503/504/offline | ErrorCard: plain message, `trace_id` (copyable), Retry, Report (beacon) |
| Stale success | ETag 304 | No visual change; freshness chip "Updated Xs ago" |

All states are **explicit React branches** (no implicit undefined rendering); covered by TC-E-xx in 04.

---

## 6. Error Taxonomy and Retry Policy (server)

| Class | Codes | Retryable | Server strategy | Client strategy |
|---|---|---|---|---|
| Validation | 400/404/422 | no | reject fast, no side effects | surface field errors |
| Throttle | 429 | yes | token bucket | honor Retry-After |
| Not ready | 409 | yes | single-flight backfill | poll with backoff |
| Transient upstream | provider timeouts, 5xx | yes | ≤3 retries w/ backoff; circuit breaker | surfaced as 503/504 retryable |
| Fatal upstream | 4xx, unit mismatch, bad schema | no | run failed_fatal; region failed | "preparation failed" + support trace |
| Data unavailable | coverage gates | n/a | 200 partial/suppressed (honest) | DataQuality UI |
| Infra | 500/503 (DB) | yes | log + trace; readiness probe fails | ErrorCard + auto-retry ×3 |

**Backoff:** FE retries: 1s, 3s, 9s (+20% jitter), max 3, only on retryable codes and network errors; never retried automatically on 422/404.

---

## 7. Pagination and Filtering

| Endpoint | Filtering | Pagination | Notes |
|---|---|---|---|
| `/analysis/events` | `region_id, hazard, start_year, end_year, min_duration, sort` | offset: `page` (1-based), `page_size` (≤100, default 25); response `total` | Stable sort `start_date` + `_id` tiebreak (EC-20); FE table: "Page X of Y · N events" |
| `/analysis/timeseries` | region/hazard/range | none — one row per year (≤ 76 rows) | No pagination needed (bounded by V-02/V-03) |
| `/admin/ingest-runs` | `region_id` | same offset scheme | Admin only |
| `/regions/presets` | `type`, (client-side text filter) | none (≤ 30 items) | Static list |

**Decision:** offset pagination (not cursor) — event counts are small (tens per region), offset is cache-friendly and trivially explainable in interview; cursor pagination is an `[EXTENSION]` if counts ever grow.

---

## 8. Integration Failure Handling Summary (external systems)

| Integration | Failure modes | Detection | Handling | Degradation |
|---|---|---|---|---|
| Open-Meteo Archive | timeout, 429, 5xx, malformed JSON, shifted schema, unit drift | httpx + Pydantic row validation | retry/backoff/honor Retry-After; circuit breaker; fatal on schema/unit | region `failed` honest; other regions unaffected; UI card |
| Open-Meteo Geocoding | timeout, 429, empty results, schema drift | same | retry ×1; fatal → 503 search | manual bbox entry fully functional |
| MongoDB | down, slow, topology change | ping timeout 2 s on readyz; exec timeouts | 503 + retry_after; readiness fails | full outage (honest); no partial writes (transactions) |
| Vercel↔Render network | FE can't reach API | fetch errors | offline UX + retries | last-viewed charts remain in memory |
| Platform (free tier cold start) | first request slow (Render spins down) | latency | FE generous first-load timeout (60 s) + spinner copy "Waking service…" | none needed |

---

## 9. Performance Budgets (NFR-03/06)

| Path | Budget | How met |
|---|---|---|
| Analysis GET warm | p95 < 300 ms | precomputed stats (DEC-24), indexed reads, L2/ETag |
| Analysis GET cold (small gap ≤ 1 y) | ≤ `ONDEMAND_WAIT_SECONDS` (8 s) or 409-poll | bounded on-demand ingest |
| Full first backfill (city, 1950–2025) | ≤ 60 s | ≤ 8 provider calls (10-year chunks); measured ~6 s; thresholds ~1 s |
| Full first backfill (bbox 3×3, 1950–2025) | ≤ 6 min | 8 batched calls ≈ 2.6k weighted calls; free-tier 600/min pacing allows ~2 chunks/min (docs/02 §5.3); shared-IP quota can push it to the next hour |
| FE first content | < 2.5 s on 4G | static SPA on CDN; skeleton UI |
| Provider load | ≤ 4 concurrent, ≤ 10 rps global | semaphore + throttle |
| DB load | ≤ ~50 writes/s peak nightly | bulk upserts batched 5k docs |

---

## 10. Data Consistency Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Stats visible before events committed | summary/series mismatch | single transaction; `data_version` bump last (02 §13) |
| Stale L2 payload after ingest | wrong headline | key includes `data_version`; explicit purge on commit |
| Two ingests interleaved | torn events | one-running-run-per-region partial unique index + asyncio lock |
| Threshold recomputed with different baseline than events used | systematic misclassification | `threshold_cache.baseline` recorded and echoed in `meta.baseline`; mismatch triggers recompute guard |
| Rounding drift between summary % and series | user distrust | single source: both computed from same annual rows in one request context; render-only rounding (EC-28) |
| Free-text region name collisions (two "Springfield") | user confusion | presets include admin1/country; bbox regions show coordinates |
| Clock skew instance↔provider | future-dated rows | reject rows with `date > today` (provider anomaly); log |
| FE race: fast filter toggling | out-of-order render | react-query keying + AbortController (CF-13) |

---

## 11. Security-Relevant Flows (pointer)

Full treatment in [`05-…`](./05-security-and-implementation-checklist.md) §1; flow-level guarantees already stated: input validation (02 §8), rate limiting (02 §11), admin constant-time token (02 §9), SSRF-free providers (02 §5.3), no secrets client-side, CORS allowlist, error envelopes without stack traces (02 §7.2).
