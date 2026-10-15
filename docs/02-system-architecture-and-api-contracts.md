# 02 — System Architecture and API Contracts

**Normative.** Requirement IDs (FR/NFR), decision IDs (DEC), and OPEN IDs refer to [`01-requirements-analysis-and-decisions.md`](./01-requirements-analysis-and-decisions.md). Edge-case IDs (EC) and flows (CF) refer to [`03-flows-edge-cases-and-failure-handling.md`](./03-flows-edge-cases-and-failure-handling.md).

---

## 1. Goals and Quality Attributes

| Attribute | Target | Driven by |
|---|---|---|
| Correctness | Heatwave detection exactly matches DEC-03 rule; unit-tested to the definition | FR-09, NFR-02 |
| Honesty | No fabricated data; coverage and significance always surfaced | NFR-05 |
| Read latency | Analysis GETs p95 < 300 ms warm (precomputed reads) | DEC-24, NFR-03 |
| Resilience | Provider failures retried with backoff, circuit-broken, never crash the API | NFR-03, NFR-01 |
| Safety | Validated inputs, rate limits, no secrets in client, no SSRF surface | NFR-06 |
| Extensibility | Hazard registry + provider interface allow new hazards/sources without API breaks | DEC-19, OOS-01 |

---

## 2. System Architecture

```
┌────────────────────────────  Browser (SPA)  ────────────────────────────┐
│ React 18 + TS + Vite                                                    │
│  RegionSelector │ HazardSelector │ RangePicker │ Charts (Chart.js)      │
│  MapPicker (Leaflet, bonus) │ SummaryCard │ Export (bonus)              │
└───────────────┬─────────────────────────────────────────────────────────┘
                │ HTTPS JSON  (GET /api/v1/*, POST /api/v1/regions)
                │ Client cache: react-query (staleTime 60s)
┌───────────────▼─────────────────────────────────────────────────────────┐
│ FastAPI app (Render free web service, single instance)                  │
│                                                                         │
│  Middleware: request-id → CORS → rate-limit → security headers          │
│  Routers: /healthz /readyz /regions /hazards /analysis/*  /admin/*      │
│  Services:                                                              │
│    RegionService      – register/resolve region, trigger backfill       │
│    AnalysisService    – read precomputed stats, assemble responses      │
│    SummaryService     – headline + trend stats (DEC-07)                 │
│    IngestService      – provider fetch → validate → persist → compute   │
│  Hazard engine (pure funcs): percentile thresholds, detector, trends    │
│  Provider client: WeatherProvider → OpenMeteoProvider (retries,         │
│    timeouts, circuit breaker, unit assertions)                           │
│  Caches: response TTL+ETag │ geocode TTL │ single-flight registry       │
│  Scheduler: APScheduler – daily backfill, recompute, cache purge        │
└───────┬───────────────────────────────────┬─────────────────────────────┘
        │ async driver (Motor)              │ HTTPS (outbound)
┌───────▼───────────────────┐    ┌──────────▼────────────────────────────┐
│ MongoDB Atlas M0          │    │ Open-Meteo Historical Weather API    │
│  regions                  │    │ Open-Meteo Geocoding API             │
│  daily_weather            │    │ (NOAA NCEI / Copernicus CDS: future  │
│  hazard_events            │    │  adapters behind same interface)     │
│  hazard_annual_stats      │    └───────────────────────────────────────┘
│  threshold_cache          │
│  ingest_runs              │
└───────────────────────────┘
```

**Deployment topology (DEC-14/15):** frontend static bundle on Vercel; backend single Uvicorn/Render instance running API + APScheduler; Atlas M0 for persistence. Locally: `docker compose up` runs Mongo + API; frontend dev server proxies `/api` to backend.

**Request path principle (DEC-24, FR-12):** all heavy computation happens at ingest time. API GETs read precomputed documents. On-demand work on a GET is limited to: (a) assembling small responses, (b) a single-flight-triggered backfill/recompute when data is missing or stale — with bounded waiting (see CF-05 in 03).

---

## 3. Technology Stack

| Layer | Choice | Version pin policy | Rationale (DEC) |
|---|---|---|---|
| Language (BE) | Python 3.12 | `backend/pyproject.toml` + lock (`uv` or `pip-tools`) | DEC-16 |
| Web framework | FastAPI + Pydantic v2 | minor-pinned | DEC-16 |
| ASGI server | Uvicorn (single worker; `--workers` documented for future) | patch-pinned | free-tier RAM |
| HTTP client | `httpx` (async, timeouts) | minor-pinned | DEC-02 |
| DB driver | `motor` (async) + `pymongo` admin ops | minor-pinned | DEC-01 |
| Scheduler | APScheduler (AsyncIOScheduler) | minor-pinned | DEC-16 |
| Numerics | `numpy` only (OLS/MK self-implemented, DEC-22) | minor-pinned | lean install |
| Frontend | React 18, TypeScript 5, Vite 5, react-query (TanStack) | caret-pinned in lockfile | DEC-18 |
| Charts | Chart.js 4 via `react-chartjs-2` | minor-pinned | NFR-04 |
| Map (bonus) | Leaflet + `react-leaflet` | minor-pinned | BONUS-01, DEC-09 |
| Tests | pytest + pytest-asyncio + httpx ASGI transport (BE); Vitest + Testing Library (FE) | dev-pinned | see 04 |
| Lint/format | ruff + ruff-format (BE); eslint + prettier (FE) | dev-pinned | interview-friendly consistency |
| CI | GitHub Actions: lint → typecheck → unit → integration (Mongo service container) → FE build | — | gate quality |

**Conventions (greenfield — established now, per repo inspection finding in 01 §1):**
- Backend layout: `backend/app/{routers,services,providers,hazard,models,db,core}`, `backend/tests/`.
- Naming: snake_case Python, camelCase TS, `SCREAMING_SNAKE` env vars.
- Every API error uses the envelope in §7.2; every response carries `trace_id` in a header (`X-Trace-Id`) and inside error bodies.
- No business logic in routers; routers → services → pure functions.

---

## 4. Component Responsibilities

| Component | Responsibility | Must not do |
|---|---|---|
| `RegionSelector` (FE) | Preset search, manual bbox form, map bbox (bonus); emits canonical region spec | Call weather APIs directly |
| `HazardSelector` (FE) | List hazards from `/hazards`; default `heatwave` (FR-03) | Hard-code hazard list (read from API) |
| `RangePicker` (FE) | Year range with min/max validation mirror; presets (1990–2020) | Accept inverted/absurd ranges client-side (mirror BE rules) |
| `AnalysisPanel` (FE) | Orchestrates fetch via react-query; loading/empty/error/partial states | Hold chart data in ad-hoc state outside query cache |
| `Charts` (FE) | Time-series (events/yr, days/yr, intensity), rolling-mean overlay, regression line (bonus visual) | Compute statistics (server-computed only) |
| `SummaryCard` (FE) | Render headline + significance/coverage caveats | Re-derive headline |
| Router layer (BE) | Parse+validate params (Pydantic), auth check for admin, delegate to services | Data access, provider calls |
| `RegionService` | Upsert region (idempotent by hash), ensure backfill scheduled, expose status | Fetch weather itself |
| `AnalysisService` | Validate range vs availability, read annual stats, compute rolling/regression on the fly from annual rows (small), ETag generation | Recompute events (that is ingest-time) |
| `SummaryService` | Apply DEC-07 headline rules incl. honesty gates | Invent metrics not in stats |
| `IngestService` | Orchestrate provider fetch, quality pipeline, persistence, event detection, annual aggregation; write `ingest_runs` audit | Serve HTTP |
| `WeatherProvider` (interface) | `fetch_daily(region, start, end) -> list[DailyReading]`; normalize units to °C/mm; classify errors as retryable/fatal | Persist anything |
| `OpenMeteoProvider` | Concrete HTTP calls, pagination by year chunks, unit assertion | Business logic |
| Hazard engine | Pure functions: thresholds, streak detection, annual aggregation, OLS/MK, rolling mean | I/O of any kind |
| Mongo repositories | Typed access, indexes, transactions | Provider calls |
| Scheduler | Nightly incremental backfill of recent 400 days for all cached regions; threshold prewarm for new regions; requeue stale jobs | Do heavy work inline in request handlers |

---

## 5. Domain Logic Specification (Climate Engine)

### 5.1 Regional 95th-percentile threshold (FR-09, DEC-03, NFR-02)

**Definition.** For region *R* and calendar day-of-year *d* (1–365; Feb 29 folded into a synthetic day treated as Feb 28's window):

> `thr(R, d) = P95( { Tmax(R, y, d′) : y ∈ 1991..2020, d′ ∈ [d−15, d+15] (wrapping), quality=valid } )`

**Algorithm (at ingest, per region — cached in `threshold_cache`):**

```
function compute_thresholds(region_id, daily_rows):
    by_doy = group valid Tmax rows by day_of_year
    thr = {}
    for d in 1..365:
        window = doys in circular_range(d-15, d+15)     # 31 doys
        samples = concat(by_doy[w] for w in window)     # ~31 * n_years values
        if len(samples) < MIN_SAMPLES:                  # MIN_SAMPLES = 300 (≈10 yrs)
            # fallback 1: widen to ±30 days
            samples = concat over circular_range(d-30, d+30)
            fallback = "wide_window"
            if len(samples) < MIN_SAMPLES:
                # fallback 2: whole calendar month containing d
                samples = by_month(month_of(d))
                fallback = "month"
                if len(samples) < MIN_SAMPLES:
                    thr[d] = None                       # unknown threshold
                    continue
        thr[d] = percentile_nearest_rank(samples, 95)
    persist threshold_cache(region_id, baseline=1991-2020, thr, fallback_flags,
                            algo_version=ALGO_VERSION, computed_at=now)
    return thr
```

**Rules:**
- Thresholds are computed **once per region per `ALGO_VERSION`** and reused for every user range (results stay comparable; OPEN-03).
- A day whose threshold is `None` (insufficient baseline) is **unassessable**: it can neither start nor extend a heatwave; reported via `coverage_pct` (DEC-20).
- Baseline gaps (missing Tmax in 1991–2020) simply reduce the sample; they are never interpolated.
- `percentile_nearest_rank` = `sorted(samples)[ceil(0.95*n) − 1]` (documented, dependency-free, deterministic).

### 5.2 Region → representative series (DEC-10, OPEN-11)

| Region type | Representation |
|---|---|
| `city` | Single geocoded point (lat, lon) |
| `state` / `bbox` | 3×3 lat/lon grid of points inside the bbox; daily Tmax = **mean of available cell values** (≥5 of 9 cells required, else day marked `degraded_grid`/missing); threshold computed on the areal-mean series |

**Grid point formula:** for bbox `[min_lat, min_lon, max_lat, max_lon]` and fractions `f ∈ {0.1, 0.5, 0.9}`: `lat_i = min_lat + f_i·(max_lat − min_lat)`, `lon_j = min_lon + f_j·(max_lon − min_lon)` → 9 cells (row-major). Same fractions every ingest → deterministic.

City point → 1 provider call series; grid → up to 9 series combined in `IngestService` (provider supports comma-separated lat/lon batches where available; else sequential with rate-limit budget).

### 5.3 Weather ingest (FR-08, NFR-01, NFR-03)

```
function ingest_region(region_id, start, end):
    run = insert ingest_runs{region_id, start, end, status: RUNNING, started_at}
    try:
        rows = provider.fetch_daily(region, start, end)     # chunked by year
        rows = quality_pipeline(rows)                       # §5.6
        write daily_weather (bulk upsert; idempotent key region_id+date)   # §13
        thr = threshold_cache.get(region_id) or compute_thresholds(...)   # §5.1
        events = detect_heatwaves(rows, thr)                # §5.4
        replace hazard_events for [start,end] (delete+insert under tx, §13)
        annual = aggregate_annual(events, rows, range)
        upsert hazard_annual_stats (key region+year+algo_version)
        bump regions.data_version = hash(region_id, version_seq+1)
        run.status = SUCCESS; run.stats = {...counts...}
    except RetryableProviderError as e:
        run.status = FAILED; raise (caller applies backoff & circuit breaker, §14)
    except FatalProviderError as e:
        run.status = FAILED_PERMANENT; log; surface partial coverage honestly
```

**Initial backfill window (on region create):** `1950-01-01` → `31 Dec of (current_year − 1)` (DEC-04 bounds), fetched in ≤10-year chunks (retry granularity), sequential per grid point under the global concurrency-4 semaphore. Thresholds are computed after the daily rows land (they need the 1991–2020 subset), then events/stats for the full window — so a freshly `ready` region answers any valid user range without on-demand fetches. Later user requests outside coverage re-enter this path via CF-05.

**Provider client hardening (NFR-03 "safe external API access"):**
- `httpx.AsyncClient` with connect timeout 5 s, read timeout 30 s, total budget per chunk 60 s.
- Retries: max 3 attempts, exponential backoff 1s/2s/4s + jitter, only on network errors, 429, 5xx; **not** on 4xx (except 429).
- Honor `Retry-After` on 429 (cap wait 60 s); when the header is absent, wait until the boundary named in the response body (`Minutely…/Hourly…/Daily…`) if that fits the chunk budget, else fail fast with the exact resume timestamp (`failed_retryable` → sweeper/CLI re-run).
- **Weighted quota pacing:** the free tier charges every request `locations × max(1, (variables / 10) × (days / 30))` "calls" against 600/min, 5 000/h, 10 000/day **per IP** (calibrated against the live archive API 2026-10-12). The client reserves that weight *before* sending (budgets 550/min, 4 500/h, 9 500/day), sleeping across minute/hour boundaries outside the per-chunk 60 s budget; the daily budget fails fast with a resume timestamp. A 9-cell 1950–2025 backfill costs ~2.6k calls ≈ 4–5 min wall time (city ≈ 300 calls ≈ 6 s), and a shared egress IP can still force a retryable failure — rows are only persisted after a whole fetch succeeds (§13), so re-running resumes idempotently.
- Circuit breaker per provider: open after 5 consecutive failures; half-open probe after 30 s.
- API keys (if any future source needs them) read from env, never logged; Authorization header redacted in logs.
- Outbound request budget: global semaphore (max 4 concurrent provider calls) to respect provider fairness.
- User-supplied coordinates are numbers only — **no user-supplied URLs** (SSRF surface zero).

### 5.4 Heatwave detection, metrics, trends (FR-09, FR-10, FR-12, DEC-07/08/11/12/22)

```
function detect_heatwaves(rows, thr, min_days=3):
    events = []; run = []
    for row in rows_sorted_by_date:
        t = thr[row.doy]
        qualifies = (row.quality == VALID
                     and t is not None
                     and row.tmax > t)                      # strictly above
        if qualifies:
            run.append(row)
        else:
            if len(run) >= min_days: events.append(make_event(run))
            run = []                                        # gap/bad day breaks run
    if len(run) >= min_days: events.append(make_event(run))
    return events

function make_event(run):
    return { start: run[0].date, end: run[-1].date, duration_days: len(run),
             mean_exceedance: mean(r.tmax - thr(r.doy) for r in run),
             peak_exceedance: max(...),
             year: year_of(run[0].date),                    # DEC-21: start-year rule
             algo_version, data_version }
```

**Annual aggregation** (`hazard_annual_stats`, per region × year × algo_version):

| Field | Definition |
|---|---|
| `event_count` | Events with `start.year == year` (DEC-21) |
| `heatwave_days` | Days in year y belonging to any event (event spanning Dec–Jan contributes days to both years; the event itself counts once in start year — documented in README) |
| `mean_duration`, `max_duration` | Over events starting in y |
| `mean_exceedance` | Area-weighted-by-day mean exceedance of days in y inside events |
| `coverage_pct` | Valid assessable days / total days in y (0–100) |

**Trends (computed at read time from annual rows — small, O(years)):**

```
function analyze(series, range):                      # series = annual rows, gaps → year with 0 only if coverage ok
    valid = [row for row in series if row.coverage_pct >= 80]      # honesty gate
    rolling5 = rolling_mean(valid.heatwave_days, window=5, min_periods=3, center=False)  # TRAILING: value at year y = mean of the up-to-5 years ending at y; min_periods=3 → first 2 points null
    slope, intercept, r, p, stderr = ols(valid.year, valid.heatwave_days)   # slope per year → also expose per_decade = slope*10
    if method includes mann-kendall: mk = mann_kendall(valid.heatwave_days)
    if len(range) >= 6:
        first3 = mean(valid[:3].heatwave_days); last3 = mean(valid[-3:].heatwave_days)
        pct_days = (last3 - first3) / first3 * 100 if first3 > 0 else None
        # first3 == 0 and last3 == 0 → pct_days = 0 ; first3 == 0, last3 > 0 → pct_days = None + flag new_activity
        pct_events = same on event_count
    else:
        pct_* = None; flag range_too_short
    headline = render_headline(...)                    # rules in this section (§5.4)
```

**Honesty gates (NFR-05) — headline rendering rules:**

| Condition | Headline behavior |
|---|---|
| Range < 6 valid years | No % sentence; show "Record too short for a reliable trend statement" + raw series |
| `pct_days is None` (zero baseline) | "Heatwaves are a new phenomenon in this record for this region (N days in recent 3 years vs none in 1990s)" — never "∞% increase" |
| `p < 0.05` | Plain % sentence + "trend statistically significant (OLS p=…)" |
| `p ≥ 0.05` | % sentence **damped**: "…increased by ~X%, though the trend is not statistically significant over this period (p=…)" |
| `coverage_pct < 90` for any included year | Caveat chip: "Low data coverage in year(s) …; interpret with care" |
| All coverage < 80 | Headline suppressed entirely; UI shows data-quality state (empty-with-reason) |

### 5.5 Hazard registry (FR-03, DEC-19)

```python
HAZARDS = {
  "heatwave": HazardDefinition(
      id="heatwave", label="Heatwave", default=True,
      detector=detect_heatwaves, threshold=regional_p95_tmax,
      day_metric="tmax", min_days=3)
  # EXTENSION POINT: "heavy_rain", "drought" register here (OOS-01)
}
```

`GET /api/v1/hazards` returns this registry → FE renders selector with `heatwave` preselected (FR-03).

### 5.6 Data-quality pipeline (NFR-01, DEC-20)

Applied per reading before persistence; every rejection increments counters stored on `ingest_runs`:

| Check | Rule | Action |
|---|---|---|
| Unit normalization | Provider declares units; assert `tmax_c` in plausible range after conversion (°F→°C if future source) | Fail run fatally on unit mismatch (never guess) |
| Physical bounds | `tmax ∈ [−90, 70] °C`, `tmin ∈ [−95, 70] °C`, `precip ≥ 0`, `tmin ≤ tmax` | Mark day `outlier`; treat Tmax as missing for detection |
| Missing values | Provider null / absent | Day stored with `missing=true`; breaks streaks (DEC-20) |
| Duplicates | Same `region_id+date` twice | Last-write-wins bulk upsert; count in run stats |
| Coverage | Valid assessable days / total | Stored per year; drives honesty gates |
| Schema | Pydantic model validation of every row | Reject row + count; ≥5% bad rows → fail run (provider likely broken) |

---

## 6. Data Model (MongoDB — FR-11, DEC-01)

Database: `climate_analyzer`. All timestamps UTC ISO-8601. All dates `YYYY-MM-DD` strings (DEC-15).

### 6.1 `regions`

```jsonc
{
  "_id": "r_9f3a…",                    // sha256(canonical_spec)[:16] → idempotent (DEC-23)
  "kind": "city | state | bbox",
  "name": "Hyderabad",                 // display name; "" for raw bbox
  "spec": {                            // canonical, for hashing + replay
    "mode": "point" | "grid",
    "points": [{"lat": 17.385, "lon": 78.4867}],
    "bbox": [17.2, 78.3, 17.6, 78.7]   // grid regions only, [min_lat,min_lon,max_lat,max_lon]
  },
  "provider_meta": {"source": "open-meteo-geocoding", "admin1": "Telangana", "country_code": "IN"},
  "status": "backfilling | ready | failed",
  "data_version": "dv_12",
  "coverage": {"start_year": 1950, "end_year": 2025, "years": 76},
  "created_at": "...", "updated_at": "..."
}
```

**Indexes:** unique `_id` (implicit); `{status:1, updated_at:1}` (scheduler scans); `{name:"text"}` optional for admin search.

**Canonical hash (region `_id` — idempotency, DEC-23/TC-U-18):**
1. Take `spec`; **drop `name`** (renaming a region must not create a duplicate).
2. Round every coordinate to **4 decimal places, ROUND_HALF_UP** (17.3850 ≡ 17.385).
3. Serialize: `json.dumps(spec, sort_keys=True, separators=(",", ":"))`.
4. `_id = "r_" + sha256(utf8_bytes).hexdigest()[:16]`.

### 6.2 `daily_weather` (the persistent cache — FR-08, DEC-12)

```jsonc
{
  "region_id": "r_9f3a…",
  "date": "1996-07-12",
  "year": 1996, "doy": 194,
  "tmax_c": 38.2, "tmin_c": 27.1, "precip_mm": 0.0,
  "quality": "valid | missing | outlier | degraded_grid",
  "grid_cells_ok": 9,                   // grid regions
  "source": "open-meteo-archive",
  "fetched_at": "..."
}
```

**Constraints/indexes:**
- **Unique compound** `{region_id:1, date:1}` — idempotent upserts; duplicate ingestion cannot double-write.
- `{region_id:1, year:1}` — annual recompute scans.
- TTL: raw rows retained ≥ 5 years wall-clock (`expireAfterSeconds` on `fetched_at`, 5y) — the durable artifact is thresholds/events/stats; daily rows re-fetchable. *(Configurable; 0 in dev to keep fixtures.)*

### 6.3 `threshold_cache`

```jsonc
{
  "region_id": "r_9f3a…",
  "baseline": "1991-2020",
  "algo_version": "1.0.0",
  "by_doy": {"1": 29.1, "2": 29.4, "...": 0},
  "fallback_flags": {"60": "wide_window"},   // day-of-year → fallback level
  "n_unresolved_doys": 0,
  "computed_at": "..."
}
```

**Unique** `{region_id:1, algo_version:1}`. Recomputed when baseline data extends materially or algo version bumps (delete-on-bump job).

### 6.4 `hazard_events`

```jsonc
{
  "region_id": "r_9f3a…", "hazard": "heatwave",
  "start_date": "1996-05-03", "end_date": "1996-05-07",
  "year": 1996,                                // start-year rule (DEC-21)
  "duration_days": 5, "mean_exceedance": 1.9, "peak_exceedance": 3.4,
  "algo_version": "1.0.0", "data_version": "dv_12"
}
```

**Unique** `{region_id:1, hazard:1, start_date:1, algo_version:1}`; index `{region_id:1, hazard:1, year:1, duration_days:-1}` for events listing.

### 6.5 `hazard_annual_stats`

```jsonc
{
  "region_id": "r_9f3a…", "hazard": "heatwave", "year": 1996,
  "event_count": 3, "heatwave_days": 14,
  "mean_duration": 4.7, "max_duration": 7,
  "mean_exceedance": 1.6,
  "coverage_pct": 99.2,
  "algo_version": "1.0.0", "data_version": "dv_12", "computed_at": "..."
}
```

**Unique** `{region_id:1, hazard:1, year:1, algo_version:1}`; index `{region_id:1, hazard:1, year:1}`.

### 6.6 `ingest_runs` (run record)

```jsonc
{
  "_id": "…", "region_id": "r_9f3a…",
  "window": {"start": "1950-01-01", "end": "2025-12-31"},
  "status": "running | success | failed_retryable | failed_fatal",
  "attempts": 2, "trigger": "scheduler | on_demand | admin",
  "counts": {"rows_in": 27740, "rows_valid": 27501, "outliers": 12, "missing": 227, "events": 84},
  "error": {"code": "PROVIDER_TIMEOUT", "message": "..."},
  "duration_ms": 4321, "started_at": "...", "finished_at": "..."
}
```

Index `{region_id:1, started_at:-1}`. Insert-only (immutable audit).

### 6.7 `schema_version`

Singleton doc `{_id:"app", schema_version: 1, algo_version: "1.0.0"}`. Migrations are additive; `algo_version` bump triggers threshold/event/stat recompute (§14).

**Referential integrity:** Mongo has no FKs — enforced in services: every write to collections 6.2–6.5 asserts `regions` doc exists; `AnalysisService` treats orphan stats as 500-class data error (logged loudly).

---

## 7. API Contracts (FR-12, NFR-03, NFR-06, DEC-23)

Base: `/api/v1`. JSON only. All responses include `X-Trace-Id`. Times are UTC ISO-8601.

### 7.1 Endpoint summary

| Method | Path | Purpose | Auth | Rate limit | Caching |
|---|---|---|---|---|---|
| GET | `/healthz` | Liveness (process up) | none | none | `no-store` |
| GET | `/readyz` | Readiness (Mongo ping + scheduler heartbeat) | none | none | `no-store` |
| GET | `/hazards` | Hazard registry (FR-03) | none | 60/min | `max-age=3600` |
| GET | `/regions/presets` | Curated city/state list | none | 60/min | `max-age=86400` |
| GET | `/regions/search?q=` | Geocode proxy (bounded) | none | 10/min | `max-age=86400` per query |
| POST | `/regions` | Register region (city/state/bbox) → triggers backfill | none | 10/min | `no-store` |
| GET | `/regions/{region_id}` | Region status + coverage | none | 60/min | `no-cache, max-age=0` (ETag revalidate every CF-02 poll) |
| GET | `/analysis/timeseries` | Annual series + rolling mean (FR-05/10/12) | none | 60/min | `max-age=300` + ETag |
| GET | `/analysis/summary` | Headline + trend stats (FR-07/10/12) | none | 60/min | `max-age=300` + ETag |
| GET | `/analysis/events` | Paginated event list | none | 60/min | `max-age=300` + ETag |
| GET | `/analysis/inspect` | Daily rows for one event (drill-down) | none | 60/min | `max-age=3600` |
| POST | `/admin/recompute` | Force recompute/threshold rebuild | `X-Admin-Token` | 5/min | `no-store` |
| GET | `/admin/ingest-runs` | Recent ingest audit (paged) | `X-Admin-Token` | 30/min | `no-store` |
| POST | `/client-errors` | Optional FE error beacon (error-report support, see 03 §5) | none | 20/min | `no-store` |

### 7.2 Conventions

**Success:** resource JSON; list endpoints:

```jsonc
{ "items": [...], "page": 1, "page_size": 25, "total": 84, "trace_id": "tr_…" }
```

**Error envelope:**

```jsonc
{
  "error": {
    "code": "VALIDATION_ERROR",          // stable machine code
    "message": "start_year must be <= end_year",
    "details": {"field": "start_year", "reason": "…"},   // optional
    "retryable": false,
    "retry_after_seconds": null           // set on 429/503
  },
  "trace_id": "tr_…"
}
```

| HTTP | `code` | When | Retryable |
|---|---|---|---|
| 400 | `BAD_REQUEST` | Malformed JSON/query syntax | no |
| 401 | `UNAUTHORIZED` | Admin token missing/invalid (§7.5) | no |
| 404 | `REGION_NOT_FOUND` | Unknown `region_id` | no |
| 404 | `EVENT_NOT_FOUND` | Unknown event in inspect | no |
| 409 | `REGION_NOT_READY` | Region backfill in progress (`retry_after_seconds`) | yes |
| 422 | `VALIDATION_ERROR` | Schema/rule violation (details per field) | no |
| 429 | `RATE_LIMITED` | Client exceeded limit | yes |
| 500 | `INTERNAL_ERROR` | Unhandled; `trace_id` for correlation | yes |
| 502 | `PROVIDER_ERROR` | On-demand backfill hit fatal provider failure | yes (later) |
| 503 | `SERVICE_UNAVAILABLE` | DB down / circuit open | yes |
| 504 | `TIMEOUT` | On-demand backfill exceeded bounded wait | yes |

### 7.3 Region endpoints (FR-02, DEC-05)

`GET /regions/presets?type=city|state` → curated list (`items: [{id, name, kind, spec}]`). Backed by a static file `backend/app/data/presets.json` (checked into repo; coordinates hand-sourced, documented provenance in file header):

- **Cities (points, 25):** Delhi, Mumbai, Bengaluru, Hyderabad, Chennai, Kolkata, Pune, Ahmedabad, Jaipur, Lucknow, Surat, Kanpur, Nagpur, Indore, Bhopal, Patna, Vadodara, Coimbatore, Kochi, Guwahati, Bhubaneswar, Chandigarh, Amritsar, Varanasi, Mysuru.
- **States (bboxes → grid mode, 12):** Andhra Pradesh, Assam, Bihar, Chhattisgarh, Gujarat, Karnataka, Kerala, Madhya Pradesh, Maharashtra, Odisha, Rajasthan, Tamil Nadu. *(Delhi NCT/Telangana/UP/WB can be added by appending JSON entries — no code change, registry is data-driven.)*

`GET /regions/search?q=hyderabad` (proxy): `q` 2–80 chars; server calls Open-Meteo Geocoding with 3 s timeout, caches response 24 h per query, converts to our region spec. Upstream failure → `503 PROVIDER_ERROR` with `retryable: true` (FE shows "search unavailable; use manual coordinates").

`POST /regions`:

```jsonc
// request (city/state preset already known by id → just reuse; raw specs:)
{ "kind": "bbox",
  "name": "My box",
  "bbox": {"min_lat": 17.1, "min_lon": 78.3, "max_lat": 17.7, "max_lon": 78.9} }
// or { "kind": "city", "name": "Hyderabad", "point": {"lat": 17.385, "lon": 78.4867} }
```

Response `201` (first time) or `200` (already existed — **idempotent** via `_id = sha256(canonical_spec)`):

```jsonc
{ "region_id": "r_9f3a…", "status": "backfilling",
  "status_url": "/api/v1/regions/r_9f3a…",
  "poll_after_seconds": 5, "trace_id": "tr_…" }
```

### 7.4 Analysis endpoints (FR-04/05/07/10/12)

Common query schema (all analysis GETs):

| Param | Type | Rules | Default |
|---|---|---|---|
| `region_id` | string | required; must exist (404 otherwise) | — |
| `hazard` | enum | optional; ∈ `/hazards` ids | `heatwave` |
| `start_year` | int | 1950 ≤ y ≤ current_year−1 (or = current_year only with `include_current_year=true`) | 1990 |
| `end_year` | int | `start_year ≤ y ≤ current_year−1` (≤ current_year with flag) | 2020 |
| `include_current_year` | bool | include partial current year, flagged | false |
| `method` | enum | `ols` \| `mann-kendall` \| `both` | `ols` |
| `page`, `page_size` | int | events endpoint only; `1 ≤ page`, `1 ≤ page_size ≤ 100` | 1, 25 |
| `min_duration` | int | events only; ≥1 | none |
| `sort` | enum | events only: `start_date:asc\|desc` | `start_date:desc` |

Validation failures → `422 VALIDATION_ERROR` with per-field details (never a stack trace).

**FE shareable links** mirror these params on the SPA root: `/?region_id=r_9f3a…&hazard=heatwave&start_year=1990&end_year=2020` (react-query keys derive from the same canonical params; `page`/`min_duration`/`sort` are not URL-synced in v1).

**`GET /analysis/timeseries`** (FR-05, FR-12):

```jsonc
{
  "region": {"region_id": "r_…", "name": "Hyderabad", "kind": "city"},
  "hazard": "heatwave",
  "range": {"start_year": 1990, "end_year": 2020, "include_current_year": false},
  "series": [
    {"year": 1990, "event_count": 2, "heatwave_days": 7, "mean_duration": 3.5,
     "max_duration": 4, "mean_exceedance": 1.2, "coverage_pct": 99.7},
    "... one row per year (missing years → row with zeros and coverage_pct from data, flagged) "
  ],
  "rolling_mean": {"window": 5, "align": "trailing", "metric": "heatwave_days",
                   "values": [{"year": 1992, "value": 6.4}, "..."]},
  "meta": {
    "algo_version": "1.0.0", "data_version": "dv_12",
    "generated_at": "2026-10-12T09:00:00Z",
    "source": "open-meteo-archive", "baseline": "1991-2020",
    "cache": "hit | miss | stale",
    "caveats": ["coverage: 99.1% of days valid", "..."]
  },
  "trace_id": "tr_…"
}
```

**`GET /analysis/summary`** (FR-07, DEC-07):

```jsonc
{
  "headline": "Heatwaves have increased by 40% in this region since 1990.",
  "headline_kind": "pct_increase | pct_decrease | no_change | new_activity | range_too_short | suppressed_low_coverage",
  "stats": {
    "pct_change_days": 40.0, "pct_change_events": 33.3,
    "baseline_window": {"start_year": 1990, "end_year": 1992, "mean_days": 5.0},
    "recent_window":    {"start_year": 2018, "end_year": 2020, "mean_days": 7.0},
    "ols": {"slope_per_decade": 0.42, "intercept": 3.1, "r": 0.31, "p_value": 0.08, "stderr": 0.22},
    "mann_kendall": null,            // populated when method includes it
    "totals": {"events": 84, "days": 291, "longest_event_days": 9}
  },
  "caveats": ["OLS p=0.08: trend not significant at 0.05", "1997 coverage 71% (gap in source data)"],
  "meta": { "...same block as timeseries..." },
  "trace_id": "tr_…"
}
```

**`GET /analysis/events`** — paginated `hazard_events` filtered by region/hazard/range/`min_duration`, sorted per `sort` (stable: `_id` as tiebreaker). `total` is the filtered count.

**`GET /analysis/inspect?region_id&hazard&start_date`** — daily rows (`date, tmax_c, threshold, qualifies, quality`) for one event; used by FE drill-down; 404 if no event starts that day.

### 7.5 Admin (DEC-06)

`POST /admin/recompute` body `{region_id, rebuild_thresholds?: bool}`; requires `X-Admin-Token` matching env (compared with `hmac.compare_digest`); wrong/missing → `401 UNAUTHORIZED` (envelope). Enqueues recompute; returns run id. `GET /admin/ingest-runs?region_id&page` lists audit docs.

---

## 8. Validation Rules (NFR-06)

Central Pydantic models; mirrored client-side (UX only — server is authoritative).

| Rule ID | Rule | Where enforced | Failure |
|---|---|---|---|
| V-01 | `start_year ≤ end_year` | BE + FE | 422 |
| V-02 | `1950 ≤ start_year`; `end_year ≤ current_year−1` (unless `include_current_year`) | BE + FE | 422 |
| V-03 | Range span ≤ 76 years (natural bound of V-02) | BE | 422 |
| V-04 | `hazard` ∈ registry | BE (FE disables unknown) | 422 |
| V-05 | `region_id` matches `^r_[0-9a-f]{16}$` then exists | BE | 422 / 404 |
| V-06 | bbox: `−90 ≤ min_lat < max_lat ≤ 90`, `−180 ≤ min_lon < max_lon ≤ 180`, area ≤ 25 deg² (~2500 km² mid-latitude, bounds grid compute), `max_lon−min_lon ≤ 10`, `max_lat−min_lat ≤ 10` | BE + FE | 422 |
| V-07 | point: lat/lon in range | BE + FE | 422 |
| V-08 | `name`: 0–80 chars, stripped, control chars rejected | BE | 422 |
| V-09 | `q` in search: 2–80 chars, no control chars | BE | 422 |
| V-10 | `page ≥ 1`, `1 ≤ page_size ≤ 100` | BE + FE | 422 |
| V-11 | `min_duration ≥ 1` | BE | 422 |
| V-12 | `sort` ∈ allowed set | BE | 422 |
| V-13 | `method` ∈ {ols, mann-kendall, both} | BE | 422 |
| V-14 | Admin token: exactly one header value, constant-time compare | BE | 401 |
| V-15 | JSON body size ≤ 10 KB (FastAPI middleware) | BE | 413 → mapped to `BAD_REQUEST` |
| V-16 | Antimeridian-crossing bbox (`min_lon > max_lon`) | BE | 422 with explicit message (documented limitation; use two boxes) |

---

## 9. Authentication and Authorization (DEC-06, OPEN-06)

- **No user auth** — the app is a public read-only analyzer; nothing in the brief requires accounts (OOS-02).
- **Anonymous capability:** read all analysis endpoints; create regions; search geocode (rate-limited).
- **Admin capability:** recompute + audit read — static bearer token from `ADMIN_API_TOKEN` env; compared constant-time; endpoints return 401 with stable code; token never logged.
- **CORS:** allowlist `FRONTEND_ORIGIN` env (exact origins, no wildcards with credentials); non-browser clients unaffected.
- **Transport:** HTTPS only at platform edge (Render/Vercel); HSTS from platform.
- Rate limiting is the authorization-adjacent abuse control (§11).

---

## 10. Caching Architecture (FR-08, NFR-06, DEC-12)

| Layer | What | Key | TTL / invalidation |
|---|---|---|---|
| L0 Provider | Open-Meteo responses | (points, date range) — implicit via persistence | We cache **into Mongo**; no second copy |
| L1 Persistent (Mongo) | `daily_weather`, thresholds, events, annual stats | natural keys + `algo_version` | Recompute on `data_version` bump / nightly incremental |
| L2 In-process response cache | Assembled timeseries/summary/events payloads | `sha256(path + canonical query + data_version + algo_version)` | 60 s TTL; explicit purge on ingest completion for that region |
| L3 HTTP | GET responses | URL | `Cache-Control: public, max-age=300` + strong ETag (`W/"sha256…" of body`); `304` on `If-None-Match` |
| L4 Client | react-query cache | query key = canonical params | `staleTime` 60 s; refetch on window focus disabled to protect rate limits |

**Rules:**
- ETag/`data_version` coupling: any ingest bump changes body → ETag changes → clients converge (consistency risk table, 03 §10).
- `healthz`/`readyz`/admin: `no-store`.
- Negative caching: none for analysis (a not-ready region returns 409, not a cached error).
- Geocode: 24 h per normalized query string in L2 (protects upstream quota).

---

## 11. Rate Limiting (NFR-06, DEC-13)

| Scope | Limit | Burst | Response |
|---|---|---|---|
| Analysis GETs + `/regions/{id}` | 60/min/IP | token bucket cap 60 | 429 + `Retry-After` + envelope `retryable: true` |
| `POST /regions`, `/regions/search` | 10/min/IP | 10 | 429 |
| `/admin/*` | 5/min/IP | 5 | 429 |
| `/client-errors` | 20/min/IP | 20 | 429 (beacon drops silently FE-side) |
| Provider (outbound) | 4 concurrent, ≤ 1 req/100 ms global | — | client-side throttle, not user-visible |

Implementation: in-memory token bucket per (IP, bucket-class); IP from platform-set `X-Forwarded-For` first hop (documented trust boundary — single-instance behind Render proxy). Headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`. **Known limitation (documented in README):** limits reset on instance restart/redeploy and do not aggregate across instances — revisit with Redis if multi-instance (DEC-13).

---

## 12. Concurrency and Duplicate-Submission Protection

| Scenario | Mechanism |
|---|---|
| Duplicate `POST /regions` (double-click, retry after network blip) | Deterministic `_id` from canonical spec → second insert hits unique key → service fetches existing doc → returns `200` with same `region_id` (idempotent) |
| Two on-demand backfills for same region (parallel users, storm after deploy) | Unique partial index on `ingest_runs {region_id:1, status:1}` **with** partial filter `status: "running"` (one running run per region); second caller gets the existing run and waits/polls (single-flight) |
| Scheduler + on-demand racing | Same single-flight guard keyed `ingest:{region_id}` (asyncio lock + DB partial unique index as the durable backstop) |
| Ingest re-entrancy after crash | `running` runs older than 10 min are marked `failed_retryable` by a sweeper; retry is safe because all writes are idempotent upserts (§13) |
| Read-during-backfill | Region `status: backfilling` → analysis GETs return `409 REGION_NOT_READY` with `retry_after_seconds` (bounded); after `ready`, reads see complete data for covered years; years beyond coverage appear as zero-filled rows **only if** ingest completed for them, else 409/missing-year caveat (03 CF-05) |
| Client double-fire of analysis GET | GETs are idempotent; react-query dedupes identical keys in-flight; ETag short-circuits server work |
| Analysis GET while nightly incremental extends data | `data_version` bump + L2 purge; in-flight responses may be one version stale — acceptable, converges ≤ 60 s; documented (03 §10) |

**Concrete single-flight index (Mongo):**
```js
db.ingest_runs.createIndex(
  { region_id: 1, status: 1 },
  { unique: true, partialFilterExpression: { status: "running" } }
)
```
Concurrent inserts with `status: "running"` for the same `region_id` fail on this index → the losing request adopts the existing run (poll/wait) instead of starting a second one.

There are **no user-authenticated write conflicts** (no user data); the conflict surface is ingest-vs-ingest and ingest-vs-read, both handled above.

---

## 13. Transaction Boundaries and Consistency

**Unit of consistency = one `(region_id, ingest window)` recompute.** Mongo supports multi-document transactions on Atlas replica sets; local dev runs a single-node replica set (`mongod --replSet rs0`) for parity.

```
session = mongo.start_session()
with session.start_transaction():
    daily_weather.bulk_write(upserts)            # idempotent
    hazard_events.delete_many(region, window)    # replace semantics
    hazard_events.insert_many(new_events)
    hazard_annual_stats.bulk_upsert(annual_rows)
    regions.update_one({_id}, {$set: {data_version: next, status: "ready", ...})
    ingest_runs.update_one({_id}, {$set: {status: "success", ...})
session.commit_transaction()   # all-or-nothing
```

**Rules:**
- Threshold computation is written in its own transaction *before* the window recompute (it is per-region immutable while `algo_version` unchanged).
- If a transaction aborts mid-ingest: nothing persisted; run marked `failed_retryable`; retry re-fetches (provider must tolerate re-fetch; our upserts make double-fetch harmless).
- **Read-your-writes:** once `status: ready` and `data_version` bumped in the same commit, subsequent GETs see the new stats (linearizable per region via Mongo causal consistency + our single-writer-per-region guard).
- **Degraded mode:** if transactions are unavailable (unexpected topology), the service falls back to ordered non-transactional writes (daily → events → stats → region bump **last**); `data_version` bump last preserves the invariant "stats visible only when their inputs are fully written". Fallback logs a warning. (Documented; not exercised on Atlas.)
- **Cross-response consistency:** `timeseries`, `summary`, `events` all embed the same `meta.data_version`; the FE fetches them together and surfaces a caveat if versions differ mid-session (03 EC-21).

---

## 14. Background Jobs and State Machines

### 14.1 Scheduler (APScheduler, single instance)

| Job | Schedule | Behavior |
|---|---|---|
| `nightly_incremental` | 02:00 UTC daily | For each `regions` doc with `status: ready`: backfill last ~400 days (handles late-arriving source data), recompute affected years, bump `data_version` |
| `stale_run_sweeper` | every 5 min | Runs `running` older than 10 min → `failed_retryable`; regions `backfilling` with no live run → re-enqueue (bounded retries: 3) |
| `threshold_prewarm` | after region create (event-driven, not cron) | Computes thresholds then window ingest (CF-05) |
| `algo_bump_recompute` | manual/admin | On `algo_version` change: delete thresholds + stats + events for old version; re-ingest all regions (nightly chunked) |

### 14.2 Region state machine (FR-02 lifecycle)

```
POST /regions
   → [new]        status=backfilling   (data_version=dv_0, coverage empty)
   → backfill ok  status=ready         (coverage set, data_version=dv_1…)
   → fatal fail   status=failed        (error stored; retriable via next POST with
                                        same spec → re-enqueue, or admin recompute)
   → nightly      ready → ready        (data_version bumps; never leaves ready
                                        unless a fatal recompute is attempted)
```

| From | Event | To | Guard |
|---|---|---|---|
| (none) | POST valid spec | backfilling | V-01..V-07 pass |
| backfilling | ingest success | ready | thresholds + ≥1 year persisted |
| backfilling | fatal error after 3 retries | failed | run attempts exhausted |
| failed | POST same spec / admin recompute | backfilling | — |
| ready | nightly incremental success | ready | data_version bump |
| ready | nightly fatal (provider down) | ready (unchanged) | alert only; data stays valid-stale |

### 14.3 Ingest run state machine

`created → running → success | failed_retryable | failed_fatal`; retryable re-enters `running` (attempts+1, max 3); sweeper transitions orphaned `running → failed_retryable`.

### 14.4 API request state (server view)

`received → validated → rate_check → cache_lookup (HIT→respond) → data_lookup (ready?) → assemble → respond`; branch failures produce the error codes of §7.2. On-demand backfill branch bounded to `ONDEMAND_WAIT_SECONDS` (default 8 s) then `504 TIMEOUT` with `retryable: true` (FE shows "preparing data…" and auto-retries with backoff; region finishes in background — 03 CF-05).

---

## 15. Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `MONGODB_URI` | `mongodb://localhost:27017` (dev) | DB connection (secret in prod) |
| `MONGODB_DB` | `climate_analyzer` | DB name |
| `ADMIN_API_TOKEN` | — (required in prod) | Admin auth (secret) |
| `FRONTEND_ORIGIN` | `http://localhost:5173` | CORS allowlist |
| `OPENMETEO_BASE_URL` | `https://archive-api.open-meteo.com` | Provider endpoint |
| `GEOCODING_BASE_URL` | `https://geocoding-api.open-meteo.com` | Geocode endpoint |
| `PROVIDER_TIMEOUT_S` / `PROVIDER_MAX_RETRIES` | 30 / 3 | Resilience knobs |
| `ONDEMAND_WAIT_SECONDS` | 8 | Bounded wait on cache-miss analysis |
| `RATE_LIMIT_ANALYSIS_PER_MIN` | 60 | Tunables for demo/deploy |
| `RESPONSE_CACHE_TTL_S` | 60 | L2 cache |
| `BASELINE_PERIOD` | `1991-2020` | Threshold baseline (DEC-03) |
| `ALGO_VERSION` | `1.0.0` | Bump → recompute pipeline |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | Log verbosity/format |

No secrets in the repo; `.env.example` documents each key (05 §4).
