import { expect, test } from '@playwright/test'
import { HYD_SEARCH, mongosh } from './helpers'

/** CF-05 (docs/03): cache-miss on-demand backfill inside analysis — the
 * 409/504-poll UX end to end: honest preparing card while the gap cannot be
 * filled (provider quota, EC-31), then data appears on the next poll after
 * the stats land (restored mid-test). API-side branches (504 <80%, 200
 * partial ≥80%) were verified against the live stack in docs/05 P5 notes. */

const RANGE = { start: 1990, end: 2020 }

function backup(): string {
  return `
db.getCollection("_e2e_backup_cf05").deleteMany({})
db.getCollection("_e2e_backup_cf05").insertMany(db.hazard_annual_stats.find(
  {region_id: "${HYD_SEARCH}", hazard: "heatwave",
   year: {$gte: ${RANGE.start}, $lte: ${RANGE.end}}}).toArray())
db.hazard_annual_stats.deleteMany(
  {region_id: "${HYD_SEARCH}", hazard: "heatwave",
   year: {$gte: ${RANGE.start}, $lte: 2010}})
`
}

const restore = `
const docs = db.getCollection("_e2e_backup_cf05").find({}).toArray()
if (docs.length) {
  try {
    db.hazard_annual_stats.insertMany(docs, {ordered: false})
  } catch (error) {
    if (String(error).indexOf('E11000') === -1) throw error
  }
}
db.getCollection("_e2e_backup_cf05").deleteMany({})
`

test('CF-05 preparing card polls, then renders data once stats land', async ({ page }) => {
  mongosh(backup())
  try {
    await page.goto(`/?region_id=${HYD_SEARCH}&start_year=${RANGE.start}&end_year=${RANGE.end}`)

    // on-demand backfill can't complete (quota) → 504 TIMEOUT/retryable →
    // FE shows the preparing card and polls every 5 s (03 CF-05 step 4)
    await expect(page.getByTestId('preparing-card')).toBeVisible({ timeout: 20_000 })
    await expect(page.getByTestId('preparing-card')).toContainText('Polling every 5 s')

    mongosh(restore)

    // next polls find the restored stats → real analysis renders
    await expect(page.getByTestId('summary-card')).toBeVisible({ timeout: 60_000 })
    await expect(page.getByTestId('headline')).toContainText('Heatwaves')
    await expect(page.getByTestId('preparing-card')).toHaveCount(0)
    await expect(page.locator('canvas')).toBeVisible()
  } finally {
    mongosh(restore) // idempotent
  }
})
