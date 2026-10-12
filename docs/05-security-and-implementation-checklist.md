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
- ⬜ Routers: hazards, regions (presets/search/create/status), analysis (timeseries/summary/events/inspect), admin, healthz/readyz (02 §7)
- ⬜ Curated presets file `backend/app/data/presets.json` (25 cities as points, 12 states as bboxes — list in 02 §7.3)
- ⬜ Validation models (V-01..V-16) + error envelope + request-id middleware (02 §7.2, §8)
- ⬜ L2/L3 caching + ETag (02 §10); rate limiter (02 §11); CORS + security headers (05 §1.2)
- ⬜ APScheduler jobs + sweeper (02 §14)
- ⬜ TC-I-01..20 green
- **DoD:** OpenAPI at `/docs` matches 02 §7; full integration suite green against real Mongo; rate-limit + ETag verifiable via curl.

### P4 — Frontend core (~2–2.5 h) — deps: P3 (needs live API)
- ⬜ App shell + react-query setup + URL param sync (shareable analysis links)
- ⬜ RegionSelector (search + manual bbox), HazardSelector (default heatwave), RangePicker (validated)
- ⬜ AnalysisPanel with loading/empty/error/partial states (03 §5)
- ⬜ SummaryCard (headline + caveats), TimeSeriesChart (Chart.js; metric toggle; rolling overlay), EventsTable (pagination)
- ⬜ Methodology tooltip + a11y text summaries
- ⬜ TC-F-01..09 green
- **DoD:** CF-01..04, CF-07 manual script passes against staging API; no console errors.

### P5 — Integration hardening (~1 h) — deps: P3+P4
- ⬜ CF-05 on-demand path end-to-end (409-poll UX)
- ⬜ TC-E-01..09 executed (Playwright or recorded manual)
- ⬜ TC-N-01..05 run; fix blockers
- ⬜ Bug bar: no open P0/P1 defects
- **DoD:** 04 §9 completion criteria met.

### P6 — Deploy & deliverables (~1 h) — deps: P5
- ⬜ Atlas M0 + Render backend + Vercel frontend; env vars set (02 §15); secrets never in repo
- ⬜ UptimeRobot (or similar) on `/readyz`; cold-start copy in FE
- ⬜ README (outline §3 below) incl. assumptions (01 §5) + AI tools disclosure (CONSTRAINT-05)
- ⬜ Confirm OPEN-18: brief file not in public git history
- ⬜ (Optional) 3–5 min demo video
- **DoD:** deployed URL walkthrough of CF-01..07 recorded; repo public; DELIV-01..04 satisfied.

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
