# 05 — Security and Implementation Checklist

**Normative.** IDs from [`01-…decisions.md`](./01-requirements-analysis-and-decisions.md); mechanisms referenced from [`02-…api-contracts.md`](./02-system-architecture-and-api-contracts.md) and [`03-…failure-handling.md`](./03-flows-edge-cases-and-failure-handling.md).

---

## 1. Security Considerations (NFR-06, DEC-06)

### 1.1 Threat model (public, unauthenticated, free-tier)

| # | Threat | Vector | Control | Residual risk |
|---|---|---|---|---|
| S-01 | Input abuse: absurd params, injection | query/body | Pydantic validation V-01..V-16 (02 §8); anchored regex for ids; enums for hazard/sort/method; body ≤ 10 KB; dates as opaque strings (no parsing of user timestamps) | Low |
| S-02 | Resource exhaustion via huge bbox / dense grid | `POST /regions` | area ≤ 25 deg², spans ≤ 10° (V-06); 3×3 grid cap (DEC-10); rate limit 10/min | Low |
| S-03 | Compute/cache stampede DoS | parallel cold analysis | single-flight per region (02 §12); analysis 60/min/IP; bounded on-demand wait; circuit breaker | Medium on shared NAT IP (EC-17) — documented |
| S-04 | Upstream quota theft (geocode proxy abuse) | `/regions/search` | 10/min/IP; 24 h response cache; q length 2–80; no arbitrary URL forwarding (SSRF zero — only fixed base URLs + numeric coords) | Low |
| S-05 | Admin endpoint takeover | `/admin/*` | `ADMIN_API_TOKEN` env; `hmac.compare_digest`; 5/min; 401 stable envelope; token never echoed/logged | Low (token entropy is owner's duty — checklist P7) |
| S-06 | CORS misconfiguration | browser | allowlist exact `FRONTEND_ORIGIN`; no `*` with credentials; preflight cached | Low |
| S-07 | Secrets leakage | repo/logs | `.env` gitignored; `.env.example` placeholder-only; log redaction for Authorization/MONGODB_URI; CI secret scan (gitleaks optional) | Low |
| S-08 | Dependency supply chain | installs | lockfiles both stacks; pin minors; `pip-audit` + `npm audit --production` in CI (non-blocking warn initially) | Medium free-tier reality |
| S-09 | Transport attacks | network | HTTPS enforced by platforms; HSTS from Render/Vercel; no mixed content (charts static) | Low |
| S-10 | XSS via region names / error text | FE render | React auto-escaping; no `dangerouslySetInnerHTML`; name control-char rejection (V-08) | Low |
| S-11 | Clickjacking | embed | `X-Frame-Options: DENY` / CSP `frame-ancestors 'none'` middleware | Low |
| S-12 | Error oracles / stack traces | any 5xx | envelope sanitization: message + trace_id only; stack traces log-only (02 §7.2) | Low |
| S-13 | CSV/PDF export injection (bonus) | export features | client-side export; CSV cells prefixed-escaped for `=+-@` (formula injection); PDF is print-stylesheet (no server rendering) | Low |
| S-14 | Log injection / PII | request logs | structured JSON logs; user inputs escaped; no cookies/localStorage read; IP stored truncated (/24) with 30-day rotation note | Low |
| S-15 | Free-tier neighbor noise | shared egress | outbound concurrency semaphore; provider errors never bubble raw upstream bodies to clients | Low |

**Security headers middleware (all API responses):** `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'none'` on API (JSON-only), HSTS at edge. FE host sets SPA-appropriate CSP (allow self, Chart.js inline-free builds).

**Explicit non-goals (documented, DEC-06):** no user accounts → no session security, no password handling, no account-takeover surface; anonymity means rate limiting is the primary abuse boundary.

### 1.2 Data handling & privacy
- No user PII collected; no cookies; no third-party analytics.
- Logs: truncated IP (/24), trace_id, path, status, latency — retained per platform default (≤ 30 days), rotated.
- Region specs (possibly revealing user interest) are stored; they are coordinates only, no identity attached.

---

## 2. Implementation Checklist

**Legend:** ⬜ not started · 🟦 in progress · ✅ done — owner flips states; each task lists **dependencies** and **completion criteria (DoD)**. Hour estimates map to CONSTRAINT-01 (8–10 h total core; bonus after).

**Execution order:** strictly phase-by-phase — **P0 → P1 → P2 → P3 → P4 → P5 → P6 → P8**, with **P7 (bonus) only after P6**. Each phase's DoD must be met before the next starts (dependencies are hard).

### P0 — Repository bootstrap (~0.5 h) — no deps
- ✅ Git init; `.gitignore` (add `docs/FullStackDeveloper_CaseStudy_Oct26.md` per OPEN-18 **before public push**), `.env.example`, MIT `LICENSE` optional
- ✅ `backend/` (pyproject, ruff config, pytest config, FastAPI skeleton with `/healthz`)
- ✅ `frontend/` (Vite React-TS scaffold, eslint/prettier, proxy `/api` → :8000)
- ✅ `docker-compose.yml` (mongo single-node replset, api)
- ✅ CI workflow skeleton (lint placeholders)
- **DoD:** `docker compose up` + `npm run dev` render hello; CI runs.
  - ✅ Verified 2026-10-12: `ruff check`/`ruff format --check` clean, `pytest` 1 passed; `eslint` clean, `tsc --noEmit && vite build` ok, `prettier --check` clean (added `frontend/.prettierignore` for `dist/`); `docker compose up -d --build` → mongo replset `rs.status().ok == 1`, api `/healthz` → 200 `{"status":"ok"}`; `npm run dev` serves the app and proxies `/healthz` → :8000 (200).

### P1 — Data layer & provider (~1.5–2 h) — deps: P0
- ✅ Mongo repositories + schema validation + indexes (02 §6.1–6.6)
- ✅ `WeatherProvider` interface + `OpenMeteoProvider` (year-chunked fetch, units assert, timeouts, retries, breaker, weighted free-tier quota pacing) (02 §5.3)
- ✅ Quality pipeline (02 §5.6)
- ✅ IngestService with transactional persist + `ingest_runs` audit + single-flight guard (02 §12–13)
- ✅ Fixtures + TC-U-08..10, TC-I-04..06 (+ TC-U-26/27 for quota pacing and 429 classification, EC-31)
- ✅ API startup ensures validators/indexes (`lifespan` in `app/main.py`); CI runs a single-node replica set with `REQUIRE_MONGO=1`
- **DoD:** scripted `python -m app.cli backfill --bbox …` fills DB for a city; unit/integration tests green; provider failure paths proven.
  - ✅ Verified 2026-10-12: `ruff check`/`format --check` clean, `pytest` 51 passed (6 integration tests on real Mongo, incl. transactions + `$jsonSchema` rejection); `backfill --point 17.385,78.4867` → region `ready`, 27 759 daily rows, 8 decade-chunks, ~6 s; `backfill --bbox 17.1,78.3,17.7,78.9` → see note below; failure paths proven by TC-I-05 (2×500 then 200), fatal schema drift, 4xx fatality, breaker, and TC-U-26/27 (minute-pace sleep, daily-budget pre-check, hourly 429 fail-fast with resume hint, over-budget 429).
  - ⚠️ Live free-tier caveat (EC-31): Open-Meteo charges `locations × max(1, (vars/10) × (days/30))` calls per request per IP (600/min, 5k/h, 10k/day) — the shared campus egress IP exhausted the hourly quota during verification; the run fails `failed_retryable` with `retry after HH:MM` and re-running resumes idempotently (rows persist only after a whole fetch, 02 §13).

### P2 — Hazard & trend engine (~2 h) — deps: P1 (thresholds need daily rows)
- ✅ Seasonal percentile thresholds + fallback ladder + cache doc (02 §5.1)
- ✅ Detector + annual aggregation (02 §5.4) incl. year-boundary rule
- ✅ Rolling mean, OLS, MK (bonus flag), % change + headline renderer with honesty gates (02 §5.4)
- ✅ TC-U-01..07, TC-U-12..24 green (incl. property tests, TC-U-21 via hypothesis)
- **DoD:** golden-file test: synthetic region → expected events/annual/headline JSON matches reviewed fixture.
  - ✅ Verified 2026-10-12: `ruff check`/`format --check` clean, `pytest` 90 passed (39 new tests: thresholds/detector/trends/pipeline units + golden-file + real-Mongo integration wiring `HeatwavePipeline` through `IngestService` — cache miss computes/persists `threshold_cache` in its own transaction, second run hits the cache). Engine modules: `app/hazard/{thresholds,detector,annual,trends,heatwave}.py`; `IngestService` now defaults to `HeatwavePipeline` and gathers baseline rows on a cache miss (02 §5.3). Golden fixture: `backend/tests/fixtures/golden/heatwave_region.json` (7 events over 1991–2020, damped headline p=0.20).
  - 📝 Judgment calls (all documented in module docstrings): (1) `degraded_grid` days are assessable for thresholds/detection/coverage — a partial grid still carries a real areal mean (02 §5.2/§5.6, DEC-10); (2) the spec's month fallback is a subset of the ±30 window on the 365-day calendar, so it is kept verbatim but only reachable via `MIN_SAMPLES` policy changes; (3) with no threshold cache *and* no baseline rows, ingest writes daily rows only (honest empty output) rather than fabricating thresholds from a partial window; (4) `calendar_doy`'s Feb 29 fold was corrected (previously returned 60, colliding with Mar 1 in leap years — now folds to 59 per 02 §5.1).

### P3 — API layer (~1.5–2 h) — deps: P2
- ✅ Routers: hazards, regions (presets/search/create/status), analysis (timeseries/summary/events/inspect), admin, healthz/readyz (02 §7)
- ✅ Curated presets file `backend/app/data/presets.json` (25 cities as points, 12 states as bboxes — list in 02 §7.3)
- ✅ Validation models (V-01..V-16) + error envelope + request-id middleware (02 §7.2, §8)
- ✅ L2/L3 caching + ETag (02 §10); rate limiter (02 §11); CORS + security headers (05 §1.2)
- ✅ APScheduler jobs + sweeper (02 §14)
- ✅ TC-I-01..20 green
- **DoD:** OpenAPI at `/docs` matches 02 §7; full integration suite green against real Mongo; rate-limit + ETag verifiable via curl.
  - ✅ Verified 2026-10-13: `ruff check`/`format --check` clean, `REQUIRE_MONGO=1 pytest` 193 passed (53 integration + 51 new unit; full TC-I-01..20 incl. single-flight create, on-demand backfill EC-05/CF-05, L2 purge on ingest, paged events EC-21, sweeper TC-I-18, rate-limit TC-I-11, admin auth TC-I-12, dead-Mongo TC-I-14; unit TC-U-11/18/25). OpenAPI `/openapi.json` lists exactly the 16 routes of 02 §7.1 (incl. root + `/api/v1` health probes) and `/docs` serves 200. curl transcript: `GET /healthz` → 200 + `cache-control: no-store` + `x-trace-id`; `GET /api/v1/regions/presets` & `/api/v1/hazards` → strong ETag, `If-None-Match` → 304; 61× `/api/v1/hazards` → 60× 200 then 429 `RATE_LIMITED` with `Retry-After: 1`, `X-RateLimit-Limit: 60`, CORS `Access-Control-Allow-Origin` on the 429, probes unlimited; admin without/wrong token → 401, correct → 200; 20 KB body → 413 `BAD_REQUEST`; V-01/V-05/V-16 violations → 422 envelope with `details.field`; unknown region → 404 `REGION_NOT_FOUND`. Modules: `app/core/{trace,errors,middleware,ratelimit,cache,deps}.py`, `app/models/api.py`, `app/hazard/registry.py`, `app/providers/geocoding.py`, `app/services/{analysis,presets,scheduler,tasks}.py`, `app/routers/*` (6), `app/data/presets.json` (25 city + 12 state = 37).
  - 📝 Judgment calls: (1) presets bypass the 25 deg² V-06 area cap — curated state boxes ship in-repo and are trusted (02 §7.3); (2) `ApiMiddleware` sits *inside* `CORSMiddleware` so 429/413 responses still carry `Access-Control-Allow-Origin`; (3) the rate limiter is in-process per DEC-13 (resets on redeploy, no cross-instance aggregation); (4) on-demand backfill (CF-05) waits up to `ondemand_wait_seconds` then returns 504 while the background run continues — never blocks a worker for a full backfill.

### P4 — Frontend core (~2–2.5 h) — deps: P3 (needs live API)
- ✅ App shell + react-query setup + URL param sync (shareable analysis links)
- ✅ RegionSelector (search + manual bbox), HazardSelector (default heatwave), RangePicker (validated)
- ✅ AnalysisPanel with loading/empty/error/partial states (03 §5)
- ✅ SummaryCard (headline + caveats), TimeSeriesChart (Chart.js; metric toggle; rolling overlay), EventsTable (pagination)
- ✅ Methodology tooltip + a11y text summaries
- ✅ TC-F-01..09 green
- **DoD:** CF-01..04, CF-07 manual script passes against staging API; no console errors.
  - ✅ Verified 2026-10-13: `npm test` → 9 passed (TC-F-01..09; vitest + jsdom + Testing Library, fixtures/mocks in `frontend/src/test/`); `npm run lint`, `tsc --noEmit`, `npm run build`, `prettier --check` all clean; CI runs `npm test` between lint and build.
  - ✅ No console errors (M-05): `frontend/src/test/setup.tsx` fails any test that triggers `console.error`; green for all 9 tests and all 4 live-render checks.
  - ✅ Live API script: `node scripts/cf-smoke.mjs` against local `docker compose up` → **30 passed, 0 failed, 1 blocked** — CF-01 defaults/trace_id/404-clear, CF-02 search→POST→idempotent repeat→`ready` (coverage 1950–2025), CF-03 create/idempotency/V-06/V-16 rejections, CF-04 identical `data_version`, warm 18 ms avg (<300 ms NFR-03), ETag `304`, 422/404 failure envelopes, CF-07 zero-event window `no_change` + zero-filled series + `NaN|Infinity`-free payload.
  - ⚠️ 1 blocked (environment, EC-31): the bbox region's grid backfill cannot complete while the shared campus egress IP is 429'd by Open-Meteo (direct probe returns 429) — re-run `node scripts/cf-smoke.mjs` when the quota window resets; everything else in CF-03 verified.
  - ✅ Live FE render: `npm run test:live` → 4 passed (real App + react-query + fetch against the local API: CF-01 defaults, CF-04 summary/charts/events render, CF-07 EmptyState, 404 → CF-01 copy, tampered URL blocked client-side). Run after `docker compose up -d`; chart canvas mocked (jsdom has no canvas — real canvas check is browser-side/P5).
  - ✅ P5 bug bar input (found during live CF-03, **fixed in P5**): after `max_ingest_attempts` (3) of `failed_retryable`, the sweeper only logged “exhausted its retry budget” and the region stayed `backfilling` forever, violating docs/02 §14.2 (`backfilling → failed`) and 03 CF-06 (“region honestly failed”). Fixed in `backend/app/services/scheduler.py::sweep_stale_runs`: the sweeper now counts consecutive failed runs **within the current backfilling episode** (`region.updated_at`, bumped on every `backfilling` transition) and marks the region `failed` with `last_error.code = INGEST_RETRY_EXHAUSTED` + re-submit guidance; a fresh `POST /regions` starts a new episode so old failures never starve a retry. Tests: `test_sweeper_marks_exhausted_region_failed`, `test_sweeper_budget_resets_when_region_resubmitted` (and `test_sweeper_leaves_live_run_alone`) in `backend/tests/integration/test_api_ops.py` → 13 passed.
  - ✅ **Continuation pass 2026-10-13** — the full P4 gate was re-run after P5 landed and found **4 defects**, all fixed and re-verified:
    1. **CI red (`npm test`):** vitest also collected the Playwright specs (`e2e/*.spec.ts`) once P5 added them → 2 failed files / 9 passed. `frontend/vite.config.ts` now excludes `e2e/**`, `test-results/**`, `playwright-report/**` (unit suite = `src/**` only; live checks stay behind `npm run test:live`, browser suite behind `npm run test:e2e`).
    2. **`prettier --check` red:** `e2e/helpers.ts` was unformatted and Playwright's `test-results/.last-run.json` was being scanned → formatted + `.prettierignore` entries (`test-results/`, `playwright-report/`).
    3. **cf05 e2e 1/8 red → EC-33 (backend, CF-05/EC-12):** `analysis.py::ensure_ready` did `exc = None` after losing the single-flight race, so a *failed* joined run served a degraded `200 suppressed_low_coverage` for a gap below the 80% floor (spec: `504 TIMEOUT` retryable). The FE — correctly — stopped polling, so restored data never appeared (live repro: 2/3 concurrent GETs answered `200`). Fixed by adopting the joined run's recorded outcome (`_joined_failure`); `test_joined_run_failure_answers_504_not_degraded_200` (TC-I-21) went `[504, 200]` → `[504, 504]`.
    4. **Latent 500:** `ingest.py::_region_lock` cached one `asyncio.Lock` per region across event loops, so the second waiter in a fresh loop raised *"bound to a different event loop"* → `INTERNAL_ERROR` (surfaced as the `[504, 500]` variant of defect 3). Locks are now cached per (region, running loop); single-loop production behaviour is unchanged.
    - Re-verified after the fixes: `npm test` **9 passed**, `npm run lint`, `tsc --noEmit`, `npm run build`, `prettier --check` clean; `npm run test:live` **4 passed**; `npm run test:e2e` **8/8 passed (28.7 s)** incl. cf05; backend `ruff check`/`ruff format --check` clean + `REQUIRE_MONGO=1 pytest` **197 passed** (TC-I-01..21).

### P5 — Integration hardening (~1 h) — deps: P3+P4
- ✅ CF-05 on-demand path end-to-end (409-poll UX)
  - **API-level** (live Mongo backup/restore of `r_4ff4af333ad67ec1` stats): (a) years 1990–2010 removed → analysis answers `504 TIMEOUT`, `retryable: true`, `retry_after_seconds: 5`, message "Data preparation failed — please retry"; run `failed_retryable` with the honest provider error; region stays `ready`; identical across 3 sequential polls and 2 concurrent GETs (no pile-up). (b) partial restore (28/31 years, coverage 90%) → `200` with caveat "Low data coverage in year(s) 2018, 2019, 2020…". Stats restored afterwards (backup: `/tmp/opencode/cf05-stats-backup.json`).
  - **Browser**: `frontend/e2e/cf05.spec.ts` — PreparingCard shows "Polling every 5 s", data restored mid-poll → summary-card + headline within 60 s. 1/1 passed.
- ✅ TC-E-01..07 executed — Playwright **8/8 in ~20 s** (`npm run test:e2e`, chromium, workers:1, webServer :5173): E-01 preset→ready→headline+charts+request order; E-02 bbox POST→PreparingCard + grid caveat footer; E-03 range change refetch, no duplicate region POST; E-04 metric toggle `aria-pressed` + `chart-summary` text; E-05 zero-event EmptyState (CF-07); E-06 `coverage_pct=50` → DataQuality panel, no summary-card (CF-12); E-07 invalid URL params → error-card + Reset, zero analysis requests; plus cf05 poll UX (above). TC-E-08 (offline banner) / TC-E-09 (mocked 503 backfill) **not executed** — beyond 04 §9's `TC-E-01..07` criterion. *(Re-run 2026-10-13 after the EC-33 fix: **8/8 in 28.7 s**, cf05 included — it had been 1/8 red while the join path served a degraded `200`.)*
- ✅ TC-N-01..05 run — no blockers; results table (recorded in PR at submit):

  | ID | Result | Evidence |
  |---|---|---|
  | TC-N-01 | PASS | `pytest tests/unit/test_provider.py` → 16 passed: configured timeouts (`test_http_client_carries_configured_timeouts`, new), retry budget ≤3 attempts, Retry-After honored, fatal 4xx no-retry, invalid input → no outbound (`test_api_analysis.py` provider `calls == []`) — all respx-mocked |
  | TC-N-02 | PASS | `node scripts/fuzz-inputs.mjs http://localhost:8000 60` → **150,000 hostile requests/60 s**; histogram 200/201/202/404/405/413/422/429 (+4×503 geocode = CF-02 spec'd degradation, EC-31); **0 unhandled 5xx, 0 hangs** (10 s hang threshold); 4xx error envelopes intact |
  | TC-N-03 | PASS | `node scripts/bench-read.mjs http://127.0.0.1:8001 60 20` (own uvicorn, `RATE_LIMIT_ANALYSIS_PER_MIN=1000000` per 02 §15; k6 unavailable → dependency-free equivalent): 24,175 ok / 0 errors; p50 48.7 / p90 67.7 / **p95 75.0 ms** / p99 92.0 / max 132 ms — NFR-03 (<300 ms) met |
  | TC-N-04 | PASS | `node scripts/burst-limit.mjs http://localhost:8000 500` → exactly 60×200 + 440×429; all 429s carry `retry-after` + `x-ratelimit-limit: 60` + `remaining: 0` + `RATE_LIMITED` envelope; `healthz` 200 after burst |
  | TC-N-05 | PASS | Provider outage live all session (EC-31): analysis/backfill runs → `failed_retryable`, scheduler retries then region honestly `failed`/`INGEST_RETRY_EXHAUSTED`; API stayed up (`healthz`/`readyz` 200); 18 failure-path tests green (`pytest -k "fail or retry or breaker or provider or quota or stale or budget or resume"`); DB-down covered by `test_db_down_still_answers_healthz_and_503s_everything_else` |
- ✅ CI API-level smoke (04 §1 requires it): `backend/scripts/seed_smoke_region.py` (deterministic ready region + 31 years stats, **offline — no provider**) + `scripts/api-smoke.mjs` (**18 checks**: healthz/readyz, hazard registry, presets, region read + 404 envelope, summary/timeseries/events with NaN guard + ETag `304` + rate-limit headers, 422 envelopes, beacon 202/422) wired into `.github/workflows/ci.yml` backend job. Verified locally **18/18**, including a clean-room fresh-DB + fresh-uvicorn run.
- ✅ Bug bar: no open P0/P1 defects — **4 defects found this cycle, all fixed** (1) sweeper retry-budget exhaustion → region `failed` + `INGEST_RETRY_EXHAUSTED`, tests + live proof on `r_c47e7a34fd6c1fec`; (2) missing bbox grid caveat in FE; (3) single-flight loser serving a degraded `200` for a sub-80% gap → **EC-33**, TC-I-21 + cf05 e2e; (4) loop-bound `_region_lock` cache → `INTERNAL_ERROR` for the second waiter in a new event loop. Remaining: EC-31 Open-Meteo 429 from campus IP (environment, documented; all resulting paths are spec'd degradations).
- **DoD:** 04 §9 met — TC-U/TC-I green in CI (real Mongo; TC-I-01..21, 197 backend tests), TC-F green in CI, TC-E-01..07 + cf05 executed and re-run green **8/8 (28.7 s)** after the EC-33 fix, TC-N-01..05 results table above, new ECs (EC-32, EC-33) added to 03 §4 + mapped in 04 §7.

### P6 — Deploy & deliverables (~1 h) — deps: P5
- 🟦 Atlas M0 + Render backend + Vercel frontend; env vars set (02 §15); secrets never in repo
  - ✅ **Frontend live:** https://climate-analyzer-seven.vercel.app (Vercel CLI deploy from `frontend/`, `frontend/vercel.json` = Vite preset → `dist`, immutable asset headers). `.vercel/` gitignored.
  - ✅ Atlas M0 + Render: **backend live 2026-10-14** — https://climate-analyzer-api-akx8.onrender.com (`srv-db7kouc9v7es73cdngug`, Docker build of `Climate-Analyzer-Backend`, `/readyz` health check, auto-deploy, env vars set via Render API: `MONGODB_URI`/`MONGODB_DB`/`ADMIN_API_TOKEN`/`FRONTEND_ORIGIN`/`LOG_*`). Atlas `clusterchat.jzckndt.mongodb.net` reachable (validators + indexes applied via `python -m app.cli indexes`); `VITE_API_BASE_URL` set on Vercel + prod redeploy done. `/healthz`, `/readyz`, `/api/v1/hazards` all 200; CORS preflight echoes the exact Vercel origin with `X-Trace-Id`/`ETag`/`X-RateLimit-*` exposed.
  - ✅ Secrets never in repo: `.env` gitignored (placeholders only), `ADMIN_API_TOKEN` generated per deploy, `.vercel/` ignored; verification grep in 06 §6.
- ✅ UptimeRobot (or similar) on `/readyz`; cold-start copy in FE
  - Cold-start copy done: `ColdStartCard` (`frontend/src/components/states.tsx`) renders on network/503 (`isColdStartError`, `lib/queries.ts`) from App boot (hazards) and the analysis panel, auto-checking every 5 s for ~90 s; wired cross-origin via `VITE_API_BASE_URL` (`lib/api.ts`) so rate limits key on the real client IP. Test: TC-F-11 (2 cases) → `npm test` 11 passed.
  - ⬜ UptimeRobot monitor creation (external account) — instructions in 06 §5.
- ✅ **UI redesign — "instrument ledger"** (2026-10-14, presentation only; no pinned selector/text changed)
  - `frontend/src/index.css` rewritten around 5 colours + 2 data hues: cool-grey ground, white plates, hairline rules; Newsreader (display) + IBM Plex Sans (interface); petrol-teal for actions/focus; **thermal colour reserved for heat** (summary plate's left rule, chart bars; weak years = the same heat washed out; teal rolling mean, ink dashed OLS). Flow simplified: **Region first, then Hazard + Period**. Chart palette follows `prefers-color-scheme`; `matchMedia` guarded for jsdom.
  - Gates: `npm test` 11 passed, `eslint`/`prettier` clean, `tsc --noEmit` + `vite build` clean, Playwright 8/8 (CF-05 flaked once on shared-region timing, passed on re-run). Screenshots (light + dark) reviewed before commit.
- ✅ **Cold-start spot check (live)** — 15 min idle → first `/healthz` 200 in 22.9 s, next 0.5 s (06 §7.1 row 8); DEC-14's ≤60 s budget holds.
- ✅ README (outline §3 below) incl. assumptions (01 §5) + AI tools disclosure (CONSTRAINT-05)
  - `README.md` — all 12 sections: summary, setup+env table, architecture diagram + stack table, Open-Meteo attribution/DEC-02, hazard logic (threshold formula, streak/year-boundary rules, honesty-gate table, headline formula), API endpoint table + curl + error envelope, caching layers + rate limits + DEC-13 limitation, assumptions 1–9 (01 §5 + cold-start note), "what I'd improve" (grid-cell percentiles, Theil–Sen, multi-source, Redis limits, cursor pagination), AI disclosure (factual, owner-completes note), testing commands + coverage summary, deployment table.
- ✅ Confirm OPEN-18: brief file not in public git history
  - Verified 2026-10-14: `git log --all --full-history --name-only` and per-commit `ls-tree` across all 15 commits → no `docs/FullStackDeveloper_CaseStudy_Oct26.md`; object-store scan of every blob found only 6 hits for the filename, all `.gitignore`/docs *references* (`.gitignore:7`, `docs/01`, `docs/05`) — never the brief's content. File stays local and gitignored.
- ⬜ (Optional) 3–5 min demo video
- ✅ **Post-deploy CF-01..07 walkthrough recorded** (06 §7.1, run 2026-10-14 against the live URL): `cf-smoke.mjs` → 29 checks green + separate burst-rate-limit (48×200 / 72×429 with `Retry-After` + `RATE_LIMITED` envelope) + live CF-06 evidence (bbox region → `failed`/`INGEST_RETRY_EXHAUSTED` after the EC-31 quota window, API stayed healthy). **CF-05 verified live** the same day once EC-35/36 shipped (06 §7.1 row 5): 2025–2026 range → on-demand backfill → `200` in 8.4 s with an honest `suppressed_low_coverage` caveat for the partial current year. Cold-start spot check after 15 min idle: below.
- ✅ **EC-34 found and fixed during the walkthrough** — Cloudflare downgrades our strong ETag to `W/"…"` on gzip, and `middleware.py::_matches` failed RFC 9110 §13.1.2 weak comparison, so gzip-only clients on the deployed URL got `200` instead of `304` (brotli/Chrome passed, which is why it never showed locally). Fix: opaque-tag comparison; regression tests TC-U-28 (13 unit) + TC-I-22 (integration); full suite green, ruff clean.
- ✅ **EC-35 + EC-36 found by re-running CF-05 on the live URL** — (a) a current-year range builds a `Jan 1 … Dec 31` window the archive refuses with `400`, which is fatal in our client, so CF-05 answered `502 PROVIDER_ERROR`; `ingest.run` now clamps to today and records an honest no-op for wholly future windows. (b) the fix then exposed that coverage was derived from the *last window only* — the live Hyderabad region's `1950–2025` collapsed to `2025–2026`; coverage is now recomputed from every stored year (`coverage_extent`) inside the write transaction. Regressions TC-I-23 + TC-I-24; full suite **214 passed**, ruff clean.
- ✅ **EC-37 found and fixed from the live "Preparing data for Mumbai…" report** — the FE poll budget was keyed on region `created_at` (a reload/shared link polled zero times) and hard-stopped after 2 min with no restart path; `Cache-Control: public, max-age=30` on `GET /regions/{id}` let the browser answer 5 s polls from HTTP cache so a ready flip sat unseen for up to 30 s; retryable backfills left `last_error=null`, indistinguishable from progress. Fix: first-observation poll clock degrading 5 s → 15 s (never stops) + "still preparing" copy + focus refetch + `last_error` surfaced; status endpoint `no-cache, max-age=0` (ETag/304 kept); `_fail` records `last_error` while staying `backfilling`. Regressions TC-I-25/26 + TC-F-11/12 (10 FE tests); full suite **216 passed**, ruff + eslint + tsc clean.
- ✅ **Production latency characterized** (06 §7.2): warm `summary` ≈ 0.8 s from Hyderabad vs p95 75 ms at the origin — CDN→Oregon floor (~0.3 s) + ~0.2 s per Atlas round trip + free-tier CPU sharing; NFR-03 holds at the origin, not through the free-tier geography. Recorded in README §7 (assumption 10)/§8; lever order: Atlas → `us-west-2`, paid instance, region-local API.
- **DoD:** deployed URL walkthrough of CF-01..07 recorded; repo public; DELIV-01..04 satisfied.
  - 🟦 **Walkthrough recorded** (06 §7.1/§7.2). Frontend + backend URLs live (HTML/JS/CSS 200; `/healthz` + `/readyz` 200, Mongo + scheduler ok); secrets-verification grep clean; repo pushed (`main` → `Climate-Analyzer-Frontend-`, backend split → `Climate-Analyzer-Backend`, CI green). Remaining: make the repos public (DELIV-01 link), UptimeRobot monitor (config in 06 §5, owner creates it), optional demo video.

### P7 — Bonus (only after P6) — CONSTRAINT-04
- ⬜ BONUS-01 Leaflet box select + region display (TC-F-10, TC-E-10)
- ⬜ BONUS-02 CSV export (series+summary) + PDF via print stylesheet (TC-E-11); CSV formula-escape (S-13)
  - CSV columns: `year, event_count, heatwave_days, mean_duration_days, max_duration_days, mean_exceedance_c, coverage_pct` + header block with region, range, headline, pct changes, OLS slope/p-value, `algo_version`, `data_version`
  - PDF: browser print view of the analysis panel (summary card + charts), no server rendering
- ⬜ BONUS-03 Mann–Kendall exposed via `method=mann-kendall|both` (TC-U-19)
- **DoD:** each bonus independently toggleable; core untouched.

### P8 — Final review (~0.5 h) — deps: P6 (and P7 if attempted)
- ⬜ Re-run all automated suites (04) and record results in the PR
- ⬜ Manual M-01..M-06
- ⬜ Security pass: TC-N-02, TC-N-06, header inspection
- ⬜ README "what I'd improve": grid-cell percentiles (DEC-10), Theil–Sen, multi-source (NOAA/CDS adapters), Redis-backed rate limits, cursor pagination
- **DoD:** submission form ready (repo, URL, README, AI disclosure, video link).

**Time-box guardrails (CONSTRAINT-01/02):** if behind at end of P4, cut line = skip bonus entirely, shrink preset list, reduce default range to 1990–2020 only (still meets FR-04), skip `/analysis/inspect`. Never cut: honesty gates, rate limiting, validation, README assumptions — they are graded dimensions.

---

## 3. README Outline (deliverable DELIV-03 — write during P6)

1. One-paragraph product summary + screenshots
2. Setup: prerequisites, `docker compose up`, env vars, seed/demo region
3. Architecture: diagram (02 §2 condensed), tech stack table
4. Data source: Open-Meteo Archive (attribution), coverage limits, why chosen (DEC-02)
5. Hazard logic: DEC-03 threshold method, streak rule, year-boundary rule, honesty gates (with formula)
6. API: endpoint table + example curl + error envelope
7. Caching & rate limits: layers + numbers + limitations (single instance)
8. Assumptions: copy of 01 §5
9. What I'd improve with more time: from P8 list
10. AI tools used and how (CONSTRAINT-05 — factual disclosure)
11. Testing: how to run suites; coverage summary
12. Deployment: live URL, environments table

---

## 4. Configuration & Secrets Checklist (P6)

| Item | Prod value source | Notes |
|---|---|---|
| `MONGODB_URI` | Atlas credential (rotate if ever exposed) | never in repo; prefer env injection |
| `ADMIN_API_TOKEN` | `openssl rand -hex 32` | min 32 bytes entropy |
| `FRONTEND_ORIGIN` | Vercel URL | exact origin |
| `OPENMETEO_BASE_URL` / `GEOCODING_BASE_URL` | defaults | overridable for tests |
| Rate/cache/wait tunables | defaults from 02 §15 | tune down if 429s annoy demo |
| Atlas network access | `0.0.0.0/0` (free tier reality) | auth via strong credentials; note in README; tighten to Render egress if IP allowlist available |

---

## 5. Known Gaps and Deviations (honest register — refresh at P8)

| Gap | Why | Plan |
|---|---|---|
| Single weather source (OPEN-02/DEC-02) | Time budget; key-less reliability | Provider interface ready; NOAA/CDS adapters listed as improvement |
| Region = areal mean, not cell-wise percentiles (DEC-10) | Effort | Documented; improvement item |
| Rate limits/process caches single-instance (DEC-13) | Free tier | Documented; Redis path noted |
| No user auth (DEC-06) | Unspecified + ungraded | Rate limits as abuse control |
| Brief confidentiality vs public repo (OPEN-18) | Requirements tension | Gitignore brief pre-push; flag to owner |
| Multi-hazard (OOS-01) | CONSTRAINT-02 scope rule | Registry extension point |
| Browser E2E in CI | time | API smoke in CI; browser suite manual/pre-submit |
| AI tools list (CONSTRAINT-05) | owner-specific | placeholder in README until completed |

*This section must be re-verified at P8 against the final code; any new gap discovered must be added to 01 (OPEN/DEC) and 04 (test matrices) in the same change.*
