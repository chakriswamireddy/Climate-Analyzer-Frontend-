# Climate Hazard Trend Analyzer

Historical **heatwave frequency, duration and intensity** for any city, state or bounding box in India — computed from the [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api) archive, served by a FastAPI + MongoDB API, and visualized with Chart.js in a React SPA. Pick a region, pick a year range, and the app reports *how heatwave days changed* with a headline number **and the caveats that keep it honest* (coverage gaps, statistical significance, suppressed trends).

> **Screenshots** _(add before submission)_ — app with a city selected showing the summary headline, the annual heatwave-days chart with the 5-year rolling mean, and the events table.

**Live app:** https://climate-analyzer-seven.vercel.app _(frontend; API URL below)_

---

## 1. Setup

### Prerequisites

- Docker + Docker Compose (MongoDB 7 single-node replica set)
- Python 3.12, Node 20+

### Run everything locally

```bash
# 1. API + MongoDB
docker compose up -d --build
curl http://localhost:8000/healthz          # {"status":"ok"}

# 2. Seed a demo region (offline, no provider calls — same script CI uses)
cd backend && python scripts/seed_smoke_region.py && cd ..

# 3. Frontend (proxies /api → :8000)
cd frontend && npm ci && npm run dev        # http://localhost:5173
```

For a **real** region (live provider fetch), instead of the seed script:

```bash
cd backend
python -m app.cli backfill --point 17.385,78.4867        # city point ≈ 6 s
python -m app.cli backfill --bbox 17.1,78.3,17.7,78.9    # 3×3 grid ≈ 4–5 min
```

### Environment variables

Copy `.env.example` → `.env`. All keys are documented in [`docs/02…`](docs/02-system-architecture-and-api-contracts.md) §15; the ones that matter:

| Var | Default | Purpose |
|---|---|---|
| `MONGODB_URI` | `mongodb://localhost:27017` | DB connection (**secret** in prod) |
| `MONGODB_DB` | `climate_analyzer` | DB name |
| `ADMIN_API_TOKEN` | — | Admin bearer token (**secret**, `openssl rand -hex 32`) |
| `FRONTEND_ORIGIN` | `http://localhost:5173` | Exact CORS allowlist origin |
| `OPENMETEO_BASE_URL` / `GEOCODING_BASE_URL` | Open-Meteo endpoints | overridable for tests |
| `RATE_LIMIT_*`, `RESPONSE_CACHE_TTL_S`, `ONDEMAND_WAIT_SECONDS` | see 02 §15 | tuning knobs |
| `VITE_API_BASE_URL` (frontend) | empty (dev proxy) | absolute API origin on deploy |

No secrets live in this repository: `.env` is gitignored, `.env.example` holds placeholders only.

---

## 2. Architecture

```
 Browser (React 18 + TS + Vite SPA)
   RegionSelector │ HazardSelector │ RangePicker │ SummaryCard │ Chart.js
        │  HTTPS JSON: GET /api/v1/*, POST /api/v1/regions
        ▼
 FastAPI (single Uvicorn instance, APScheduler in-process)
   middleware: request-id → CORS → rate limit → security headers
   routers: /healthz /readyz /regions /hazards /analysis/* /admin/*
   services: RegionService · AnalysisService · SummaryService · IngestService
   hazard engine (pure functions): thresholds · detector · annual agg · trends
   provider client: WeatherProvider → OpenMeteoProvider (retry, breaker, quota pacing)
   caches: response TTL + ETag │ geocode TTL │ single-flight per region
        │                              │
        ▼ (Motor, transactions)        ▼ (outbound HTTPS)
 MongoDB (Atlas M0 / Docker)        Open-Meteo Archive + Geocoding
```

**Request-path principle:** all heavy computation happens at **ingest time** (thresholds, event detection, annual aggregation). API reads are document lookups plus small in-memory rollups; the only on-demand work is a *single-flight, bounded* backfill when data for the requested range is missing.

| Layer | Choice |
|---|---|
| Backend | Python 3.12, FastAPI, Pydantic v2, Uvicorn, APScheduler |
| DB | MongoDB (Motor async driver), `$jsonSchema` validation, transactions |
| Numerics | numpy only (OLS + Mann–Kendall self-implemented) |
| Frontend | React 18, TypeScript, Vite, TanStack Query |
| Charts | Chart.js 4 via react-chartjs-2 |
| Tests | pytest + hypothesis + respx (BE) · Vitest + Testing Library (FE) · Playwright (E2E) |
| Lint/format | ruff + ruff-format (BE) · ESLint + Prettier (FE) |
| CI | GitHub Actions: lint → tests (real Mongo) → API smoke → FE build |

Full design documents live in [`docs/`](docs/) (requirements & decisions, architecture & API contracts, flows & failure handling, test matrices, security checklist).

---

## 3. Data source

- **Open-Meteo Historical Weather Archive** — daily `temperature_2m_max` etc., 1940→present, global, **no API key**, CORS-friendly, cited as required by the brief.
- **Why this source** (decision DEC-02): NOAA NCEI needs station selection + a token; Copernicus CDS needs an account, API keys and large NetCDF downloads. Open-Meteo is the lowest-risk way to satisfy the brief inside the time budget, and the provider is behind a `WeatherProvider` interface, so a NOAA/CDS adapter can be added without touching the engine.
- **Coverage limits:** the archive is daily gridded reanalysis, not station observations; coverage degrades before ~1950 (we start at 1950 by default and warn for 1940–1949). Free-tier rate limits are per IP and are paced client-side.
- **Quality handling:** physically implausible values (Tmax/Tmin outside −90…70 °C, negative precipitation) are flagged as outliers and treated as missing — never silently dropped, never interpolated. Missing days break consecutive-day runs (documented below).

---

## 4. Hazard logic

**Definition (heatwave):** ≥ 3 consecutive days with daily maximum temperature **strictly above** the region's calendar-day **95th percentile**, computed on a **1991–2020 baseline with a ±15-day window** around each day-of-year (WMO-normal style; the same spirit as the ETCCDI TX90p index).

**Threshold (computed once per region, cached):**

```
thr(R, d) = P95( { Tmax(R, y, d') : y ∈ 1991..2020, d' ∈ [d−15, d+15] circular, valid } )
```

- `P95` = nearest-rank percentile: `sorted(samples)[ceil(0.95·n) − 1]` — deterministic and dependency-free.
- Fallback ladder when a window has < 300 samples: widen to ±30 days → whole calendar month → *unknown* (that day is unassessable and can neither start nor extend a heatwave).
- Feb 29 folds into Feb 28's window. Thresholds are frozen per region per `algo_version` so results stay comparable across ranges.

**Detection rules:**

- A qualifying run of ≥ 3 days is one event; a missing/outlier day **breaks** the run.
- Events are assigned to the calendar year of their **start date**; a Dec→Jan streak is one event (counted once) while its *days* are counted in each year they fall in.
- **Intensity** = mean daily exceedance (`Tmax − threshold`) per event; **frequency** = events/year and heatwave-days/year.

**Honesty gates (headline rendering):**

| Condition | What the headline does |
|---|---|
| Range shorter than 6 valid years | no % claim — "record too short for a reliable trend statement" |
| Baseline mean = 0 but recent > 0 | "new phenomenon in this record" — never `∞%` |
| OLS `p < 0.05` | plain % sentence + "trend statistically significant (p=…)" |
| OLS `p ≥ 0.05` | % sentence **damped**: "…though the trend is not statistically significant (p=…)" |
| Any year with coverage < 90 % | coverage caveat chip listing the years |
| All years below 80 % coverage | headline suppressed entirely; the UI shows the data-quality state instead |

**Headline formula:** `% change = (mean heatwave-days/yr over last 3 years − mean over first 3 years) / mean over first 3 years × 100`, always shown next to the OLS slope/p-value and a 5-year trailing rolling mean.

**Known simplification:** a state/box is reduced to **one representative series** — the areal mean of a 3×3 grid of points inside the box (≥5 of 9 cells must be valid). Percentile thresholds are then computed on that areal-mean series, not per grid cell (see §8).

---

## 5. API

Base path `/api/v1`, JSON only, every response carries `X-Trace-Id`. Interactive docs at `/docs`.

| Method | Path | Purpose | Rate limit | Caching |
|---|---|---|---|---|
| GET | `/healthz` | liveness (no DB) | none | `no-store` |
| GET | `/readyz` | readiness (Mongo ping + scheduler heartbeat) | none | `no-store` |
| GET | `/hazards` | hazard registry | 60/min | 1 h |
| GET | `/regions/presets` | curated 25 cities + 12 states | 60/min | 24 h |
| GET | `/regions/search?q=` | geocode proxy (2–80 chars) | 10/min | 24 h/query |
| POST | `/regions` | register region (idempotent) → triggers backfill | 10/min | `no-store` |
| GET | `/regions/{region_id}` | status + coverage | 60/min | 30 s |
| GET | `/analysis/timeseries` | annual series + 5-yr rolling mean | 60/min | 300 s + ETag |
| GET | `/analysis/summary` | headline + trend stats | 60/min | 300 s + ETag |
| GET | `/analysis/events` | paginated event list | 60/min | 300 s + ETag |
| GET | `/analysis/inspect` | daily rows for one event | 60/min | 1 h |
| POST | `/admin/recompute` | force recompute (`X-Admin-Token`) | 5/min | `no-store` |
| GET | `/admin/ingest-runs` | ingest audit (`X-Admin-Token`) | 30/min | `no-store` |
| POST | `/client-errors` | optional FE error beacon | 20/min | `no-store` |

```bash
# register a city and read its summary
REGION=$(curl -s -X POST http://localhost:8000/api/v1/regions \
  -H 'Content-Type: application/json' \
  -d '{"kind":"city","name":"Hyderabad","point":{"lat":17.385,"lon":78.4867}}')

curl -s "http://localhost:8000/api/v1/analysis/summary?region_id=r_XXXX&hazard=heatwave&start_year=1990&end_year=2020"
```

**Errors** always use one envelope (stack traces are logged server-side only):

```jsonc
{
  "error": {
    "code": "VALIDATION_ERROR",           // stable machine code
    "message": "start_year must be <= end_year",
    "details": {"field": "start_year", "reason": "…"},
    "retryable": false,
    "retry_after_seconds": null           // set on 429/503/504
  },
  "trace_id": "tr_…"
}
```

Codes: `BAD_REQUEST` 400 · `UNAUTHORIZED` 401 · `REGION_NOT_FOUND` 404 · `REGION_NOT_READY` 409 (retryable) · `VALIDATION_ERROR` 422 · `RATE_LIMITED` 429 · `INTERNAL_ERROR` 500 · `SERVICE_UNAVAILABLE` 503 · `TIMEOUT` 504 (on-demand backfill exceeded its bounded wait — safe to retry).

---

## 6. Caching & rate limits

| Layer | What | Policy |
|---|---|---|
| Persistent (Mongo) | daily rows, thresholds, events, annual stats | recomputed on `data_version` bump / nightly incremental job |
| In-process response cache | assembled payloads | 60 s TTL, keyed by `(path, canonical query, data_version, algo_version)`, purged on ingest |
| HTTP | GET responses | `Cache-Control: public, max-age=300` + strong `ETag` → `304` on `If-None-Match` |
| Client | TanStack Query | `staleTime` 60 s, no refetch-on-focus (protects the rate budget) |
| Geocode | search results | 24 h per normalized query |

Rate limits are in-memory token buckets per `(IP, class)`: **60/min** analysis reads, **10/min** region create + geocode, **5/min** admin, 20/min beacon. `429` responses carry `Retry-After` and `X-RateLimit-*` headers.

**Documented limitation (DEC-13):** limits and caches are process-local — they reset on redeploy and do not aggregate across instances. That is fine for a single-instance free-tier deploy; a Redis-backed limiter is the noted upgrade path.

---

## 7. Assumptions

1. Heatwave = ≥3 consecutive days with Tmax strictly above the region's calendar-day 95th percentile (baseline 1991–2020, ±15-day window).
2. Single weather provider (Open-Meteo Historical); the provider layer is an interface, so other sources are a drop-in addition.
3. Regions are evaluated as an areal-mean series (3×3 grid for state/box), not per-cell percentiles.
4. No user authentication — the API is public and abuse is controlled with per-IP rate limiting; one admin endpoint uses a static bearer token from env.
5. The current (partial) year is excluded from analysis by default.
6. Only the heatwave hazard is implemented; the hazard registry is data-driven so more can be added without API changes.
7. Free-tier, single-instance deployment: rate limits and caches are process-local.
8. Dates are stored as provider-local calendar date strings (`YYYY-MM-DD`) — no timezone arithmetic on daily climate data.
9. Free-tier services sleep when idle: the first request after idle can take up to ~60 s. The UI detects this and shows an auto-retrying "waking up the API" card instead of an error.
10. Production latency is geo-bound, not code-bound: from India the path is CDN edge → Render (Oregon) → Atlas, which costs a ~0.3 s floor plus ~0.2 s per Mongo round trip (warm reads ≈ 0.8 s). The < 300 ms NFR holds at the origin (p95 75 ms locally, TC-N-03); measurements and the fix order are in [`docs/06-deploy-runbook.md` §7.2](docs/06-deploy-runbook.md).

---

## 8. What I'd improve with more time

- **Grid-cell percentiles** — compute thresholds per cell and combine (majority/area-weighted) instead of percentiles of the areal mean; the current approach is a documented simplification.
- **Theil–Sen slope + seasonal Mann–Kendall** alongside OLS for outlier-robust trends (Mann–Kendall is already implemented behind a flag).
- **Multi-source data** — NOAA NCEI / Copernicus CDS adapters behind the existing `WeatherProvider` interface, with cross-source validation.
- **Redis-backed rate limits and response cache** so limits survive deploys and aggregate across instances.
- **Cursor pagination** for the events list instead of page numbers (cheaper on large event sets).
- **Colocate the data tier** — Atlas M0 next to the API region (and a paid instance without CPU sharing) to reclaim the ~0.5 s of cross-region round trips measured on the live URL (§7.10).
- Map-based region selection and CSV/PDF export (bonus items, deliberately deferred under the scope-over-polish rule).

---

## 9. AI tools used and how

> **Disclosure placeholder — CONSTRAINT-05 requires a factual list before submission.**

- **Claude (opencode agent)** — used for scaffolding, implementing and refactoring code across backend, frontend and tests; writing and iterating on the design docs in `docs/`; drafting README sections; and running/interpreting the test suites.
- The human owner reviewed requirements, made the final decisions recorded in `docs/01` (DEC/OPEN registers), chose the climate methodology, and verified every phase against its definition-of-done.

_(Owner: fill in any other tools used, e.g. ChatGPT/GitHub Copilot, before submitting.)_

---

## 10. Testing

```bash
# Backend — unit + integration against a real Mongo replica set
cd backend
pip install -e ".[dev]"
REQUIRE_MONGO=1 TEST_MONGODB_URI="mongodb://localhost:27017/?directConnection=true" pytest -q
ruff check . && ruff format --check .

# API smoke (18 checks, offline seed — also runs in CI)
python scripts/seed_smoke_region.py
python -m uvicorn app.main:app --port 8000 &
node scripts/api-smoke.mjs http://127.0.0.1:8000 <region_id>

# Frontend — component tests, types, lint, build
cd frontend
npm ci && npm test && npm run lint && npm run build

# End-to-end (Playwright, TC-E-01..07 + CF-05) and live render checks
npm run test:e2e
npm run test:live          # requires the API running locally

# Non-functional helpers (need a running API)
node scripts/fuzz-inputs.mjs http://localhost:8000 60   # hostile-input fuzz
node scripts/bench-read.mjs http://127.0.0.1:8001 60 20 # read latency
node scripts/burst-limit.mjs http://localhost:8000 500  # rate-limit burst
```

Coverage summary: 214 backend tests (unit + integration, TC-U-01..28 and TC-I-01..24), 11 frontend component tests (TC-F-01..11), 8 Playwright flows, an 18-check API smoke in CI, plus TC-N-01..05 (safe external access, 150k-request fuzz with zero unhandled 5xx, p95 read latency 75 ms, exact rate-limit behaviour, provider-outage resilience). Test matrices and IDs: [`docs/04-test-matrices.md`](docs/04-test-matrices.md).

---

## 11. Deployment

| Environment | URL | Notes |
|---|---|---|
| Frontend (Vercel) | https://climate-analyzer-seven.vercel.app | static Vite build; `VITE_API_BASE_URL` points at the API |
| Backend (Render) | https://climate-analyzer-api-akx8.onrender.com | Docker web service, health check `/readyz`, free tier sleeps when idle; `render.yaml` blueprint, auto-deploy on |
| Database (MongoDB Atlas M0) | — | `MONGODB_URI` injected as a secret; network `0.0.0.0/0` (free-tier reality), auth via strong credentials |
| Monitoring | UptimeRobot on `/readyz` | external liveness, not part of the app — monitor config in [`docs/06-deploy-runbook.md` §5](docs/06-deploy-runbook.md) (owner creates it in their account) |

Local development never touches production: `docker compose up` runs Mongo + API on `localhost:8000`, and the frontend dev server proxies `/api` there.

Step-by-step deployment instructions, environment variables and the secrets checklist: **[`docs/06-deploy-runbook.md`](docs/06-deploy-runbook.md)**.

---

## 12. License & confidentiality

MIT — see [`LICENSE`](LICENSE). The assignment brief itself is **not** published in this repository (it is gitignored and absent from the git history).
