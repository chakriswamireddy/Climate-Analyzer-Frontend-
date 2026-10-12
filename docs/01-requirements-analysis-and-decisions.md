# 01 — Requirements Analysis, Ambiguities, and Implementation Decisions

**Project:** Climate Hazard Trend Analyzer (case study, Dygnify Ventures, Oct 2026)
**Source of truth:** [`docs/FullStackDeveloper_CaseStudy_Oct26.md`](./FullStackDeveloper_CaseStudy_Oct26.md)
**Status:** Normative for implementation. Any deviation from this file requires updating the test matrices in [`04-test-matrices.md`](./04-test-matrices.md).

---

## 0. How to Read This Document Set

| File | Contents |
|---|---|
| `01-requirements-analysis-and-decisions.md` (this file) | Requirement inventory (FR/NFR/CONSTRAINT/DELIVERABLE IDs), ambiguities (OPEN IDs), implementation decisions (DEC IDs), out-of-scope items |
| `02-system-architecture-and-api-contracts.md` | Architecture, components, tech stack, data model, hazard/trend algorithms, API contracts, validation rules, state machines, caching, rate limits, concurrency, transactions |
| `03-flows-edge-cases-and-failure-handling.md` | End-to-end and critical flows (CF IDs) with pre/postconditions and failure paths, edge-case matrix (EC IDs), error taxonomy, loading/empty states, pagination/filtering, integration failure handling |
| `04-test-matrices.md` | Unit/integration/E2E/non-functional test matrices with expected results, edge-case→test mapping, manual review checklist |
| `05-security-and-implementation-checklist.md` | Security considerations, phased implementation checklist with dependencies and completion criteria |

**Claim markers used throughout:**

- `[REQ]` — directly stated in the case study (cited by line range in the source file).
- `[EVAL]` — implied by the Evaluation Dimensions table (a graded expectation, not a feature).
- `[OPEN]` — ambiguous or unstated in the case study; needs an assumption or a decision.
- `[DECISION]` — an implementation decision made by the developer to resolve an OPEN item; each is numbered (`DEC-xx`) and has a rationale + alternatives considered.
- `[EXTENSION]` — explicitly out of scope for the core submission; documented for future work / "what I'd improve" README section.

> **Rule (per case study line 12–13):** No requirement below is invented. Every `[DECISION]` maps to an `[OPEN]` or is a neutral engineering choice (stack, tooling) that the case study leaves free. Every assumption must also appear in the project README (CONSTRAINT-02).

---

## 1. Repository Inspection Findings

Inspected before writing this plan (2026-10-12):

- `backend/` — **empty directory**.
- `frontend/` — **empty directory**.
- `docs/` — contains only the case-study brief.
- No `README`, no package manifests (`package.json`, `requirements.txt`, `pyproject.toml`), no CI config, no Dockerfiles, no `.git` repository.

**Consequence:** there are **no existing architecture, technology stack, coding conventions, or established patterns to follow**. The stack proposed in `02-…api-contracts.md` §3 is therefore a `[DECISION]` grounded in (a) explicit stack constraints in the brief (React frontend, Python backend, NoSQL database, Chart.js/D3-class charting), (b) the free-tier deployment targets the brief names (Vercel, Netlify, Firebase Hosting, Render, Cloud Run), and (c) the 8–10 hour effort budget (CONSTRAINT-01). This is a deviation risk only if the reviewer expected an existing codebase — there is none.

---

## 2. Requirement Inventory

### 2.1 Functional Requirements (Core)

| ID | Requirement (faithful restatement) | Source (brief lines) |
|---|---|---|
| FR-01 | Web application that analyzes historical weather data and visualizes trends in climate hazards (heatwaves, droughts, heavy rainfall *as examples*) for a selected region over time. | 18–20 |
| FR-02 | Frontend: user can select a geographic region — city, state, **or** a custom lat-lon box. | 24 |
| FR-03 | Frontend: hazard type selector offering **Heatwave, selected by default**. | 25 |
| FR-04 | Frontend: user can choose a time range (e.g. 1990–2020). | 26 |
| FR-05 | Display: time-series plots of hazard **frequency and intensity**. | 28 |
| FR-06 | Display: interactive visualizations (charts, maps). | 29 |
| FR-07 | Display: summary insights in plain language, e.g. "Heatwaves have increased by 40% in this region since 1990." | 30 |
| FR-08 | Backend (Python): ingest **and cache** historical weather data from public APIs — NOAA NCEI, Copernicus CDS, Open-Meteo, **or any other source**. | 31–35 |
| FR-09 | Backend: hazard-detection logic. Heatwave definition given as example: **3+ consecutive days with maximum temperature above the 95th percentile for that region**. | 36–39 |
| FR-10 | Backend: analyze **frequency and duration** trends using **rolling averages or regression** (either is acceptable; both is stronger). | 40 |
| FR-11 | Backend: store the data in a **NoSQL database**. | 41 |
| FR-12 | Backend: on an API call, **read from the database** (not live from the provider) and return **JSON summaries and trend plots**. | 42–43 |

### 2.2 Non-Functional Requirements (from Evaluation Dimensions — each is graded)

| ID | Requirement | Brief weight | Source |
|---|---|---|---|
| NFR-01 | Data handling: real-world noisy, inconsistent data — **gaps, outliers, units, caching** must all be visibly handled. | 20% | 47–49 |
| NFR-02 | Climate logic: ability to define and apply domain-specific rules (percentile-based heatwave definition) — correctness and defensibility of the climate methodology. | 20% | 51–53 |
| NFR-03 | Backend skills: efficient data processing, clean API design, **safe external API access**. | 20% | 55–57 |
| NFR-04 | Frontend skills: clear, interactive visualization using **Chart.js, D3.js, or similar**. | 15% | 59–61 |
| NFR-05 | Critical thinking: derive **sound, honest** insight from climate indicators (no overclaiming from noisy data). | 10% | 63 |
| NFR-06 | Security + performance: **API caching, rate-limiting, and safe handling of user inputs**. | 15% | 64–66 |

### 2.3 Bonus Requirements (Optional; +10 points; only after core works — CONSTRAINT-04)

| ID | Requirement | Source |
|---|---|---|
| BONUS-01 | Map UI (e.g. Leaflet or Mapbox) for region selection. | 68 |
| BONUS-02 | PDF/CSV export of insights. | 69 |
| BONUS-03 | Trend detection techniques such as linear regression or the Mann-Kendall test. | 70 |

### 2.4 Constraints

| ID | Constraint | Source |
|---|---|---|
| CONSTRAINT-01 | Effort budget ≈ 8–10 hours of focused work; deadline external to this repo (LinkedIn post). | 6–7 |
| CONSTRAINT-02 | "Scope over polish": a working, well-reasoned heatwave analysis beats a half-finished multi-hazard app. **State any assumptions in README.** | 11–13 |
| CONSTRAINT-03 | Confidentiality: **do not publish or share the brief**; the code repository itself may be public. | 16–17 |
| CONSTRAINT-04 | Bonus challenges only after core features work. | 66–67 |
| CONSTRAINT-05 | AI tools permitted; must be disclosed in README + submission form; candidate must walk through code and modify it live in interview. | 14–15 |
| CONSTRAINT-06 | Submission is via an external form (submit once; resubmission with same email replaces). Late submissions may not be reviewed. | 8–10, 81 |

### 2.5 Deliverables (outside application code; tracked for completeness)

| ID | Deliverable | Source |
|---|---|---|
| DELIV-01 | Public GitHub repository link. | 71 |
| DELIV-02 | Deployed app URL (free tier acceptable). | 72–74 |
| DELIV-03 | README covering: setup steps, architecture, data source(s), hazard logic, assumptions, what I'd improve with more time. | 75–76 |
| DELIV-04 | AI tools used and how (README + form). | 77 |
| DELIV-05 | Optional: 3–5 minute demo video. | 78 |

### 2.6 Explicitly Out of Scope

| ID | Item | Justification |
|---|---|---|
| OOS-01 | Drought and heavy-rainfall detection logic. | FR-01 lists them only as examples of hazard types; FR-03 fixes Heatwave as *the* hazard type for the app; CONSTRAINT-02 explicitly favors a finished heatwave analysis over a half-finished multi-hazard app. The hazard engine is designed as a pluggable registry so these can be added without rework (see 02 §5.5). |
| OOS-02 | User accounts, personalization, saved analyses. | Never mentioned anywhere in the brief. See OPEN-06. |
| OOS-03 | Mobile-native apps. | Brief asks for a web application only. Responsive web is in scope. |
| OOS-04 | Live/forecast weather. | Brief says *historical* weather data. |
| OOS-05 | Publishable-grade climate science (peer-reviewed indices beyond the given rule). | The brief gives the rule to implement; sophistication beyond it is a bonus, not a requirement (OPEN-03 documents baseline/seasonality choices). |

---

## 3. Ambiguity Register (OPEN items)

Each item: what is ambiguous, the resolution chosen, where it is implemented, and residual risk.

| ID | Ambiguity in the brief | Resolution | Implemented in | Residual risk |
|---|---|---|---|---|
| OPEN-01 | "NoSQL database" (FR-11) — no product named. | DEC-01: **MongoDB Atlas free tier (M0)** with `motor` async driver. | 02 §3, §6 | Low. Any document DB satisfies FR-11; Mongo is the lowest-friction free-tier option with local Docker parity. |
| OPEN-02 | Data source among NOAA NCEI / Copernicus CDS / Open-Meteo "or any other" (FR-08). NOAA NCEI needs station selection + token; CDS needs account + CDS API keys + large downloads. | DEC-02: **Open-Meteo Historical Weather API** as sole source for the submission; provider layer is an interface so NOAA/CDS adapters are `[EXTENSION]`. | 02 §5.3, §7.3 | Medium. Reviewer may value multi-source; mitigated by documenting the provider interface + why one source was chosen (time budget). |
| OPEN-03 | "95th percentile for that region" (FR-09) — percentile over which baseline period, and with or without seasonal (day-of-year) conditioning? A single annual 95th percentile would label ordinary summer days as "heatwave" days. | DEC-03: **Calendar-day percentile with ±15-day window over a fixed 1991–2020 baseline** (WMO-normal style), matching the ETCCDI TX90p spirit; threshold frozen per region (computed once, cached). Documented in README as an assumption. | 02 §5.1 | Medium. Alternative (percentile within each month; or percentile of the selected range itself) would change counts; we freeze baseline so results are comparable across ranges. |
| OPEN-04 | Time range bounds (FR-04: "e.g. 1990–2020"). | DEC-04: allow **1950–current year−1** (Open-Meteo archive covers 1940+; we leave the current partial year out to avoid misleading annual aggregates). Default UI selection **1990–2020** as in the brief example. | 02 §7.4, §8.3 | Low. |
| OPEN-05 | "city, state, or custom lat-lon box" (FR-02) — no gazetteer specified. | DEC-05: **preset list of 25 Indian cities + 12 states** (enumerated in 02 §7.3) shipped as static JSON (`backend/app/data/presets.json`), **plus** free-text search against Open-Meteo Geocoding API (server-proxied, cached), **plus** manual lat-lon-box entry and (BONUS-01) Leaflet box selection. | 02 §7.3, §8 | Low. |
| OPEN-06 | No authentication/authorization anywhere in the brief. | DEC-06: **No user auth.** Public read API; abuse controlled by **per-IP rate limiting**; one admin-only endpoint guarded by a static bearer token from env. | 02 §9, 05 §1 | Low. Adding auth would violate the time budget and is not graded. |
| OPEN-07 | "increased by 40% since 1990" (FR-07) — how exactly is % change computed, and over which metric? | DEC-07: headline uses **% change in mean annual heatwave-day count between the first 3 years and the last 3 years of the selected range** (minimum 6-year range; shorter ranges suppress the % and show an honest "record too short" message). Event-count % change is also computed and shown as a secondary figure. Trend slope from regression is always shown next to it. | 02 §5.4, §7.5 | Low-medium. Any deterministic, documented formula is defensible; honesty rules (NFR-05) matter more than the exact formula. |
| OPEN-08 | "rolling averages" (FR-10) — window length unspecified. | DEC-08: **5-year trailing rolling mean** of annual series, computed server-side; window configurable in one constant. | 02 §5.4 | Low. |
| OPEN-09 | Whether the app must *detect* hazards for drought/heavy rain (FR-01 examples) or only heatwave (FR-03). | OOS-01: heatwave only; hazard type is a registry key so the API shape already supports more (`hazard=heatwave` enum, with a documented extension point). UI shows a disabled "Coming soon" state for other hazards? — **No**: UI shows only supported hazards; the selector is future-proofed, not padded. | 02 §5.5, §8.2 | Low given CONSTRAINT-02. |
| OPEN-10 | Maps (FR-06 "charts, maps") — is a map required or is charts-only acceptable? | DEC-09: charts are the core (FR-05/07); a **Leaflet map with region bounding-box display + drag-select (BONUS-01) is treated as bonus**, attempted only after core (CONSTRAINT-04). If bonus time runs out, region is shown as a static description card. | 03 CF-03, 05 checklist P7 | Low (bonus is optional). |
| OPEN-11 | Geographic aggregation: a "state" or box is 2D; the percentile rule is defined on a single temperature series. | DEC-10: every region is reduced to **one representative time series** — city: geocoded point; state/box: **area-weighted mean of a 3×3 grid of points inside the box** (fallback: centroid if grid fetch fails for ≥2 cells → quality flag `degraded_grid`). This is a real climate-analysis simplification (areal average), documented in README. | 02 §5.2 | Medium. A stricter alternative (percentile per grid cell, then majority vote) is more correct but >2× effort; flagged in "what I'd improve". |
| OPEN-12 | "frequency/intensity" (FR-05) — intensity undefined for a binary day-threshold rule. | DEC-11: **frequency** = events/year + heatwave-days/year; **intensity** = mean daily exceedance (Tmax − threshold) per event, aggregated to annual mean intensity. Both plotted. | 02 §5.4 | Low. |
| OPEN-13 | Caching semantics (FR-08 "ingest and cache"; NFR-06 "API caching"). | DEC-12: two cache layers — (1) **persistent cache**: ingested daily data + computed events/stats in MongoDB, TTL-refreshed job recomputes; (2) **response cache**: in-process TTL cache + HTTP `Cache-Control`/`ETag` on GET analysis endpoints. | 02 §10 | Low. |
| OPEN-14 | Rate-limiting specifics (NFR-06: "rate-limiting" named, numbers unspecified). | DEC-13: 60 req/min/IP on analysis GETs; 10 req/min/IP on region create + geocode proxy; 429 + `Retry-After`. Single-process token bucket (documented limitation for multi-instance deployments). | 02 §11, 05 §1 | Low. |
| OPEN-15 | Deployment target unspecified (DELIV-02 lists examples). | DEC-14: backend on **Render free web service**; frontend on **Vercel**; MongoDB Atlas M0. Docker images kept provider-agnostic so Cloud Run/Fly work unchanged. | 05 §2 P6 | Low. |
| OPEN-16 | Timezone / "day" definition for daily Tmax and the 95th percentile. | DEC-15: provider returns **local-time daily aggregates**; store `date` as a calendar date string `YYYY-MM-DD` in the provider's local timezone of the region centroid; no DST-sensitive arithmetic is done on timestamps (dates are date-only). Documented in README. | 02 §6.1, §5.1 | Low. |
| OPEN-17 | Is the current (partial) year included when user selects range ending "now"? | DEC-04 (same item): **exclude the current partial year** by default; include only if `include_current_year=true` and respond with `partial_year: true` in meta. | 02 §7.4 | Low. |
| OPEN-18 | Whether the brief file itself may remain in the public repo (DELIV-01 public repo vs CONSTRAINT-03 "do not publish the brief"). | **Unresolved — flagged for the human owner.** Recommended: add `docs/FullStackDeveloper_CaseStudy_Oct26.md` to `.gitignore` before the first public push, keeping it local-only. This plan references it by filename only. | 05 §2 P0 (checklist item) | **High if ignored** — publishing the brief would violate CONSTRAINT-03. |

---

## 4. Implementation Decisions Register

| ID | Decision | Rationale | Alternatives considered |
|---|---|---|---|
| DEC-01 | MongoDB Atlas M0 + PyMongo/Motor; collections with `$jsonSchema` validation where useful. | FR-11 mandates NoSQL; document model fits per-region nested annual stats; free tier named-friendly; local `docker compose` parity for dev/tests. | Firestore (vendor lock-in, emulator friction), DynamoDB (local dev heavier, modeling awkward for range scans), Redis-only (not durable enough as system of record), PostgreSQL JSONB (violates "NoSQL" letter of FR-11). |
| DEC-02 | Open-Meteo Historical Weather API as the weather source; provider interface `WeatherProvider` with retries/timeouts/circuit breaker; NOAA NCEI & CDS documented as future adapters. | Free, no API key, CORS-friendly, daily tmax/tmin/precip 1940–present, global coverage — the lowest-risk way to satisfy FR-08 within CONSTRAINT-01. "Or any other source" explicitly permits it. Safe access (NFR-03) implemented in the provider client regardless of source. | NOAA NCEI (station/network selection complexity, token, US-centric GHCN), Copernicus CDS (account + API key + heavyweight NetCDF/Python client), Meteostat bulk (large local data). |
| DEC-03 | Seasonal percentile: threshold per calendar day from a ±15-day window over 1991–2020, requiring ≥15 years of valid samples per window else falling back to month-window then whole-season percentile (flagged in meta). | Without day-of-year conditioning the rule is climatologically meaningless (NFR-02). 1991–2020 is the current WMO standard normal period. ±15-day window ≈ 31-day sample per day-of-year, standard practice for percentile indices. | Percentile of the user-selected range (self-referential, unstable across ranges); fixed absolute threshold (e.g., 35 °C — not regional); monthly percentile (coarser artifacts at month boundaries). |
| DEC-04 | Range bounds 1950..(current_year−1); default 1990–2020; 1940–1949 accepted via flag but warned (lower coverage). | Open-Meteo archive 1940+; post-1950 coverage/reliability better; brief's example default is 1990–2020. Partial current year excluded (DEC-17). | 1940 hard floor with no warning; open-ended "latest". |
| DEC-05 | Static curated preset list + server-proxied Open-Meteo Geocoding (TTL-cached, rate-limited) + manual bbox form + Leaflet drag-select (bonus). | Satisfies all three region types in FR-02; proxy keeps CORS simple and lets us rate-limit/cache a third-party API (NFR-03/06). | Nominatim (heavier ToS constraints), Mapbox Geocoding (key/cost), client-direct geocoding (key exposure, uncontrolled rate). |
| DEC-06 | No user auth; per-IP rate limits; admin endpoints behind `ADMIN_API_TOKEN` (env, timing-safe compare). | Auth is nowhere in the brief; cost of building it exceeds remaining budget after graded dimensions. Rate limiting is explicitly graded (NFR-06). | Full JWT auth (ungraded scope creep), API keys for all users (friction, ungraded). |
| DEC-07 | Headline % change = (mean heatwave-days/yr of last 3 yrs − mean of first 3 yrs) / mean of first 3 yrs × 100, for ranges ≥ 6 years; division-by-zero handled (first-period mean 0 → report absolute difference + "new phenomenon" wording). Secondary metric: same for event counts. Always accompanied by regression slope + p-value. | Deterministic, simple to explain, matches the phrasing "increased by 40% … since 1990" (start-vs-end comparison). 3-year means smooth single-year noise without hiding the endpoints. Honesty requirement (NFR-05) served by pairing with statistical significance. | % change of first-vs-last single year (noise-sensitive); slope-based % (harder to explain); Theil–Sen slope (better robustness, more code — listed in "improve with more time"). |
| DEC-08 | 5-year **trailing** rolling mean (value at year y averages the up-to-5 years ending at y; `min_periods=3` → first 2 points null), server-computed on annual series. | FR-10 names rolling averages; 5 years is short enough for a ~30-year default range to remain readable; trailing avoids future leakage into past points. | 10-year (too few points on short ranges), 3-year (still noisy), centered (leaks later years backward). |
| DEC-09 | Leaflet map (box select + display) is bonus; charts are core. | Matches CONSTRAINT-04 ordering. | Leaflet core (risks unfinished core). |
| DEC-10 | Region → representative series via 3×3 grid areal mean for state/box (city = single point). | Defensible, tractable in the time budget; documented simplification; avoids false precision of centroid-only for large boxes. | Centroid-only (cheap but misleading for large states), full grid-cell percentile + majority vote (more correct, more time — "improve" item). |
| DEC-11 | Intensity = mean daily exceedance above threshold (°C). | FR-05 asks for intensity; exceedance is the natural companion of a percentile threshold. | Absolute Tmax mean (mixes climate level with anomaly); heat-index (needs humidity, out of scope). |
| DEC-12 | Persistent cache in MongoDB + in-process response TTL cache (60 s) + HTTP `Cache-Control: public, max-age=300` + ETag on GET analysis endpoints; cache key = canonical hash of (region_id, hazard, start, end, algo_version, data_version). | NFR-06 grades API caching; `data_version` prevents serving summaries computed from stale ingests (see 02 §13). | Redis for response cache (extra infra on free tier, overkill at this scale); CDN-only (depends on host). |
| DEC-13 | Token-bucket rate limiter, in-process, keyed by client IP (X-Forwarded-For trusted only from platform proxy); 60/min analysis, 10/min region/geocode; 429 + `Retry-After: <seconds>`. | NFR-06 explicitly grades rate-limiting; in-process is honest about single-instance free-tier deploys (documented limitation). | slowapi/redis (Redis free-tier adds latency+setup; revisit if multi-instance). |
| DEC-14 | Render (backend) + Vercel (frontend) + Atlas M0; both built from Dockerfile-agnostic configs. | Named in the brief as acceptable free tiers; Render supports background workers on the same free service via APScheduler in-process. | Cloud Run (needs GCP setup), Fly.io (free tier changes), Netlify for FE (equivalent; Vercel chosen for React SPA ergonomics). |
| DEC-15 | Store dates as `YYYY-MM-DD` strings (provider local calendar); never as UTC timestamps. | Avoids DST/timezone off-by-one bugs in daily climate data (EC-14 class). | UTC Date objects (DST edge cases), epoch ms (opaque, TZ-ambiguous). |
| DEC-16 | Backend framework: **FastAPI + Pydantic v2 + Uvicorn**; background work via **APScheduler** in the same process (single free-tier instance). | FastAPI gives OpenAPI docs (clean API design, NFR-03), validation for "safe handling of user inputs" (NFR-06), async for provider fan-out. APScheduler avoids a separate worker service on free tier. | Flask (no built-in validation/docs), Django (heavy), Celery+Redis (infra overkill). |
| DEC-17 | Exclude current partial year unless `include_current_year=true`; when included, mark `partial_year: true` in response meta. | Annual aggregates for a partial year are misleading (NFR-05 honesty). | Always include (noise); never include (rigid). |
| DEC-18 | Frontend stack: **React 18 + TypeScript + Vite**, charts via **Chart.js through react-chartjs-2** (brief names Chart.js first), Leaflet for bonus map, no component library beyond minimal CSS (keeps bundle small, code walk-through easy per CONSTRAINT-05). | NFR-04 names Chart.js; TS improves interview walk-through quality; Vite is the fastest path to a deployed SPA. | D3 alone (more code, harder to defend in 8–10 h), MUI/Ant (heavy), Next.js (SSR unneeded for a client-side analyzer). |
| DEC-19 | Hazard engine is a pure-function library (`hazard/`) with a `HazardDefinition` registry keyed by hazard id; heatwave is the only registered definition. API/UI iterate the registry. | Satisfies FR-09 now and keeps OOS-01 cheap to add later; pure functions = unit-testable (NFR-02). | Hard-coded heatwave-only functions. |
| DEC-20 | Anomaly/outlier policy: values outside physically-plausible bounds per variable (Tmax/Tmin ∈ [−90, 70] °C, precip ≥ 0) are treated as missing (flagged `outlier`), never silently dropped; days with missing Tmax break consecutive-day runs (documented limitation of the streak rule); response meta reports `coverage_pct` and `gaps`. | Directly serves NFR-01 (gaps, outliers, units). Open-Meteo is °C; provider adapter asserts unit metadata and converts if a source reports °F (NOAA path). | Interpolation across gaps (fabricates data — dishonest per NFR-05); treating gap as "no heatwave" silently (hides data quality). |
| DEC-21 | Events assigned to the calendar year of their **start date**; a streak spanning Dec–Jan counts as one event (no split). Annual day-counts count each heatwave day in its own calendar year (so days can straddle two years while the event counts once). | Simple, explainable, documented; avoids double-counting events. | Splitting events at year boundaries (inflates counts); assigning to end year. |
| DEC-22 | Trend statistics: ordinary least squares slope + Pearson r + two-sided p-value on annual series (numpy, no scipy dependency required); Mann–Kendall (pure-python implementation, ~40 lines) offered as BONUS-03 behind a query flag `method=mann-kendall|ols|both` (default `ols`). | FR-10 says "rolling averages or regression"; we ship both rolling + OLS. MK is explicitly a bonus item. | scipy.stats (heavier install for two coefficients), Theil–Sen (stronger but ungraded). |
| DEC-23 | API style: versioned prefix `/api/v1`, JSON:Error envelope `{error: {code, message, details?, trace_id}}` with HTTP status semantics; all analysis endpoints are **GET** (idempotent, cacheable). Region registration is `POST /api/v1/regions` — idempotent via deterministic `region_id` derived from canonicalized region spec. | Clean API design (NFR-03); idempotent POST removes duplicate-submission risk (see 02 §12). | RPC-style `POST /analyze` (not cacheable, duplicate-submission risk), GraphQL (overkill). |
| DEC-24 | All compute happens at ingest time (precomputation of events + annual stats); API reads are O(1) document lookups + tiny in-memory rollups. On-demand recompute only when `data_version` or `algo_version` mismatches, executed under a per-region lock. | NFR-03 grades efficiency; keeps p95 latency predictable on free tier; centralizes race handling in one code path (02 §12–13). | Compute-on-read with response caching only (cache stampede risk on cold keys). |

---

## 5. Assumptions Log (to be copied into README — CONSTRAINT-02)

1. Heatwave = ≥3 consecutive days with Tmax strictly above the region's calendar-day 95th percentile (baseline 1991–2020, ±15-day window) — DEC-03.
2. Single weather provider (Open-Meteo Historical) — DEC-02.
3. Regions evaluated as areal-mean series (3×3 grid for state/box) — DEC-10.
4. No user authentication — DEC-06.
5. Current partial year excluded by default — DEC-17.
6. Only heatwave hazard implemented; architecture supports more — OOS-01/DEC-19.
7. Free-tier single-instance deployment; rate limits and caches are process-local — DEC-13/14.
8. Dates stored as provider-local calendar date strings — DEC-15.
9. AI tools used: (owner fills in; CONSTRAINT-05 requires disclosure). *(Placeholder — must be completed before submission.)*
