import { execFileSync } from 'node:child_process'

/** Region fixtures in the local compose DB (docs/05 P4/P5 live verification). */
export const HYD_PRESET = 'r_3b145803cd96866d' // preset city_hyderabad → ready
export const HYD_SEARCH = 'r_4ff4af333ad67ec1' // search-created Hyderabad, 1950–2025
export const BBOX_REGION = 'r_eddb3a60c9518a62' // "Hyderabad box", promoted ready by E-02

const CONTAINER = process.env.MONGO_CONTAINER ?? 'climate-analyzer-mongo-1'

/** Run a mongosh script against the local compose Mongo.
 *
 * Two hard-won details (P5): (1) `--eval` parses the whole program, while
 * stdin/REPL mode continues line-by-line after an error and exits 0 — that
 * combination silently skipped the CF-05 stats backup once; (2) `db.<name>`
 * is undefined until the collection exists, so backup collections must be
 * opened with `db.getCollection(...)`. An uncaught script error now fails
 * the test loudly instead of leaving the DB half-mutated. */
export function mongosh(js: string): void {
  execFileSync(
    'docker',
    ['exec', '-i', CONTAINER, 'mongosh', '--quiet', 'climate_analyzer', '--eval', js],
    {
      encoding: 'utf8',
    },
  )
}

/** Promote the seeded bbox region to a ready, fully-statted analysis target
 * (idempotent — E-02b "grid caveat footer" needs a region the provider quota
 * (EC-31) can no longer backfill for real). */
export function seedReadyBbox(): void {
  mongosh(`
db.regions.updateOne(
  {_id: "${BBOX_REGION}"},
  {$set: {status: "ready", data_version: "dv_1",
          coverage: {start_year: 1990, end_year: 2020, years: 31}},
   $unset: {last_error: ""}}
)
db.hazard_annual_stats.deleteMany({region_id: "${BBOX_REGION}", hazard: "heatwave"})
const src = db.hazard_annual_stats.find(
  {region_id: "${HYD_PRESET}", hazard: "heatwave", year: {$gte: 1990, $lte: 2020}}
).toArray()
src.forEach((d) => { delete d._id; d.region_id = "${BBOX_REGION}" })
if (src.length) db.hazard_annual_stats.insertMany(src)
db.hazard_events.deleteMany({region_id: "${BBOX_REGION}", hazard: "heatwave"})
const ev = db.hazard_events.find(
  {region_id: "${HYD_PRESET}", hazard: "heatwave", year: {$in: [1991, 1992, 1993]}}
).toArray()
ev.forEach((d) => { delete d._id; d.region_id = "${BBOX_REGION}" })
if (ev.length) db.hazard_events.insertMany(ev)
`)
}
