# 06 — Deployment Runbook (P6)

Companion to [`05 §2 P6`](./05-security-and-implementation-checklist.md) and [`05 §4`](./05-security-and-implementation-checklist.md) (secrets checklist). Owner executes the account-side steps; everything else is in-repo (`render.yaml`, `frontend/vercel.json`).

**Target topology (DEC-14):** Vercel (frontend) → Render (API) → MongoDB Atlas M0, with UptimeRobot polling `/readyz`.

---

## 1. Status log

| Step | State | Evidence |
|---|---|---|
| Frontend → Vercel | ✅ deployed 2026-10-14 | https://climate-analyzer-seven.vercel.app (CLI deploy from `frontend/`, `vercel.json` = Vite preset) |
| Backend → Render | ✅ deployed 2026-10-14 | https://climate-analyzer-api-akx8.onrender.com — service `srv-db7kouc9v7es73cdngug` (Docker, `render.yaml`-equivalent settings, `/readyz` health check, auto-deploy on), built from `Climate-Analyzer-Backend@3955bfe` |
| MongoDB Atlas M0 | ✅ | cluster `clusterchat.jzckndt.mongodb.net`; `MONGODB_URI` set as a Render env var (never in git); validators + indexes applied with `python -m app.cli indexes` |
| `VITE_API_BASE_URL` set on Vercel | ✅ | set to the Render origin, then `vercel deploy --prod` (alias re-pointed 2026-10-14) |
| UptimeRobot on `/readyz` | ⬜ owner action | config below (§5) — owner creates the monitor in their account; README marks it pending |
| Cold-start copy in FE | ✅ | `ColdStartCard` (`frontend/src/components/states.tsx`), TC-F-11 |
| CF-01..07 walkthrough | ✅ 2026-10-14 | §7.1 results table + §7.2 latency; `scripts/cf-smoke.mjs <render-url>` → 29 green / 1 env-blocked (EC-31), burst rate-limit 48×200 + 72×429 verified separately |
| EC-34 ETag weak-comparison fix | ✅ 2026-10-14 | found on the live URL (gzip → `W/"…"` → `200` instead of `304`); `middleware.py::_matches` + TC-U-28/TC-I-22; suite 211 green |
| README (DELIV-03) | ✅ | [`../README.md`](../README.md) |
| OPEN-18 brief not in git history | ✅ verified | all 15 commit trees scanned; only `.gitignore`/docs filename references |
| Secrets never in repo | ✅ | `.env` gitignored, `.env.example` placeholders, `.vercel/` gitignored |

---

## 2. MongoDB Atlas M0

1. cloud.mongodb.com → create a free **M0** cluster (any region close to Render's).
2. **Database Access:** create a user with *Read and write to any database* on `climate_analyzer` (strong password; store in a password manager — this is `MONGODB_URI`).
3. **Network Access:** add `0.0.0.0/0` (free-tier reality — no static egress IP on Render free; documented in 05 §4). Auth is the strong credential; tighten to Render's egress range if the plan allows.
4. **Connect → Drivers:** copy the URI, e.g.
   `mongodb+srv://USER:PASS@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority`
   → set `MONGODB_DB=climate_analyzer`. Keep the full URI with credentials **out of git**.

## 3. Backend on Render (from `render.yaml`)

1. render.com → **New → Blueprint** → connect the (public) GitHub repo → Render reads `render.yaml`.
   - Without a repo: **New → Web Service → Docker** → point at the repo/`backend` context and paste the same env vars by hand.
2. Environment variables (already declared in the blueprint; `sync: false` values are pasted in the dashboard):

   | Var | Value source |
   |---|---|
   | `MONGODB_URI` | Atlas connection string (step 2) |
   | `MONGODB_DB` | `climate_analyzer` |
   | `ADMIN_API_TOKEN` | `openssl rand -hex 32` (or blueprint `generateValue`) |
   | `FRONTEND_ORIGIN` | exact Vercel origin: `https://climate-analyzer-seven.vercel.app` |
   | `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` |

3. First boot: the API ensures Mongo validators + indexes in `lifespan` (02 §6.7). Check `GET /healthz` → 200 and `GET /readyz` → 200 (Mongo ping + scheduler heartbeat).
   - The boot ping has a 2 s budget (02 §7.1), so a cold Atlas handshake can log `Mongo unreachable at startup` even though `/readyz` goes green seconds later. If that happens, apply the fallback once: `MONGODB_URI=<prod uri> python -m app.cli indexes` (executed 2026-10-14 — all 7 collections, validators and indexes present).
4. Warm the app: `POST /api/v1/regions` with a preset city, or run `python -m app.cli backfill --point …` once against the prod URI if you want a pre-seeded region.
5. **Free-tier cold start:** the service sleeps after ~15 min idle; the first request takes up to ~60 s. The frontend shows the "Waking up the API…" card (`ColdStartCard`, auto-checks every 5 s up to 90 s), and UptimeRobot's ping keeps the sleep window from being a surprise.

## 4. Frontend on Vercel + API wiring

Already deployed (`frontend/vercel.json`, framework preset Vite, output `dist`). To finish wiring:

```bash
cd frontend
npx vercel env add VITE_API_BASE_URL production   # https://<render-url>.onrender.com
npx vercel redeploy --prod
```

- The SPA calls the API **directly** (cross-origin) rather than through a Vercel rewrite, so per-IP rate limits key on the real client IP instead of Vercel's shared egress (DEC-13).
- CORS on the API must list that exact origin in `FRONTEND_ORIGIN` (step 3).
- Deep links are query-string only (`/?region_id=…`), so no SPA rewrites are required.

## 5. UptimeRobot (or similar) on `/readyz`

1. uptime robot → new monitor: `https://<render-url>/readyz`, interval 5 min, HTTP(s).
2. `/readyz` is the readiness probe (Mongo + scheduler), not just process liveness — an alert means "would fail real requests".
3. `/healthz` and `/readyz` are excluded from rate limiting and `no-store` (02 §7.1), so polling is free.

## 6. Secrets checklist (05 §4)

| Item | Prod value source | Notes |
|---|---|---|
| `MONGODB_URI` | Atlas credential | never in repo; rotate if ever exposed |
| `ADMIN_API_TOKEN` | `openssl rand -hex 32` | ≥32 bytes entropy; never echoed/logged |
| `FRONTEND_ORIGIN` | Vercel URL | exact origin, no trailing slash |
| `OPENMETEO_BASE_URL` / `GEOCODING_BASE_URL` | defaults | overridable for tests |
| Rate/cache/wait tunables | defaults from 02 §15 | tune down if 429s annoy the demo |
| Atlas network access | `0.0.0.0/0` (free tier) | strong credentials; note in README |

Verification: `git grep -nE "(mongodb\+srv|://[^ ]*:[^ ]*@)" -- . ':!docs'` returns nothing; `.env` absent from `git ls-files`; CI secret scan (gitleaks, optional) clean.

## 7. Post-deploy walkthrough (P6 DoD)

Record results against CF-01..07 on the deployed URL:

1. **CF-01** first load → defaults from `/hazards`, no console errors, `X-Trace-Id` on responses.
2. **CF-02** preset city → `POST /regions` → `ready` → headline + charts.
3. **CF-03** custom bbox → grid caveat footer; invalid inputs → 422 envelope.
4. **CF-04** same range twice → warm read < 300 ms, `ETag` → `304`.
5. **CF-05** range outside coverage → preparing card polling every 5 s → data appears.
6. **CF-06** provider unavailable → honest failure + retry path (region `failed` after budget).
7. **CF-07** empty range → empty state, zero-filled series, no `NaN`/`Infinity`.
8. Cold start: sleep the API (or wait 15 min) → reload → "Waking up the API…" card, then normal render.
9. Rate limit: >60 requests/min → `429` with `Retry-After` + `X-RateLimit-*`.

### 7.1 Results — 2026-10-14 (API-level against the live URL)

`node scripts/cf-smoke.mjs https://climate-analyzer-api-akx8.onrender.com` → **29 passed, 1 env-blocked, 1 latency note** (plus the checks below, run separately):

| # | Flow | Result | Evidence |
|---|---|---|---|
| 1 | CF-01 defaults | ✅ | `/hazards` 200 + `trace_id` + default `heatwave`; `/regions/presets` 200 ×37; unknown region → 404 `REGION_NOT_FOUND` envelope |
| 2 | CF-02 preset city | ✅ | search → `POST /regions` → 201 `r_4ff4af333ad67ec1`; repeat POST idempotent (`same=true`); `ready` with coverage 1950–2025 (76 y) |
| 3 | CF-03 bbox + validation | ✅ | bbox create idempotent; span>10° → 422 V-06; antimeridian → 422 V-16. Grid backfill for `r_c47e7a34fd6c1fec` was **blocked by EC-31** (shared-IP provider quota) and, after 3 attempts, the region went honestly `failed` / `INGEST_RETRY_EXHAUSTED` (EC-32 behaviour observed live). Re-submit when the quota window resets |
| 4 | CF-04 render + ETag | ✅ | `timeseries`/`summary`/`events` 200, one `data_version`, `events.total` = summary totals, honest headline, ETag → `304`; **EC-34 fix** below |
| 5 | CF-05 on-demand backfill | ⬜ | needs a range outside coverage *and* a healthy provider window (same EC-31 constraint); locally covered by TC-I-16/CF-05 e2e |
| 6 | CF-06 provider failure | ✅ (observed live) | the bbox region above: `failed` + `INGEST_RETRY_EXHAUSTED` + resubmit guidance, API stayed 200 on `/healthz`/`/readyz` (matches TC-N-05) |
| 7 | CF-07 empty range | ✅ | 1950–1951 → 200, `totals.events == 0`, `headline_kind: no_change`, zero-filled rows, no `NaN`/`Infinity` |
| 8 | Cold start | ⬜ | requires ~15 min of idle (Render sleep); FE card covered by TC-F-11 |
| 9 | Rate limit | ✅ | 120-request burst → 48×200 + 72×429; 429 body `{code: RATE_LIMITED, retryable: true, retry_after_seconds: 1}` with `Retry-After: 1`, `X-RateLimit-Limit: 60`, `X-RateLimit-Remaining: 0`; `/healthz` unaffected |

**EC-34 (found here, fixed in P6):** on the deployed URL the origin sits behind Cloudflare, which downgrades our strong `"…"` ETag to the weak `W/"…"` *whenever it gzip-encodes* (brotli passes it through, which is why `fetch`/Chrome saw `304` while `curl` saw `200`). `If-None-Match` must use the **weak** comparison (RFC 9110 §13.1.2), so `middleware.py::_matches` now compares opaque tags (strip `W/` + quotes). Local suites passed before and after; regression tests TC-U-28 + TC-I-22. Verification on the deployed URL: `If-None-Match: W/"<etag>"` → `304` empty body, wrong tag → `200`.

### 7.2 Latency on the deployed URL (NFR-03 in production)

Measured from Hyderabad through the live stack (CF edge → Render Oregon → Atlas), medians of repeated GETs:

| Endpoint | Warm TTFB | Notes |
|---|---|---|
| `/healthz` | ~0.30 s | no DB — pure geo/CDN floor |
| `/api/v1/regions/presets` | ~0.30 s | static presets |
| `/api/v1/hazards` | ~0.29 s | registry, L2-resident |
| `/readyz` | ~0.50 s | + Atlas ping ⇒ **~0.2 s per Mongo round trip** |
| `/api/v1/analysis/summary` (`cache: hit`) | ~0.80 s | + region/stats lookups on top of the floor |

So the read path is doing its job (L2 `cache: hit`, no provider calls) but three network hops dominate: **~0.30 s CDN→Oregon floor**, **~0.2 s per Atlas round trip**, plus free-tier CPU sharing. NFR-03's **p95 < 300 ms holds at the origin** (TC-N-03: p95 75 ms, p99 92 ms on localhost); from India on the free-tier geography it cannot. The levers, in order of effect: put **Atlas in `us-west-2` next to Render** (recurring round trips shrink ~0.2 s each → ~0.5 s reads), then a paid Render instance, then a region-local frontend+API. Recorded in README §7 (assumption 10) and §8.

