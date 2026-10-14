import { expect, test, type Page } from '@playwright/test'
import { BBOX_REGION, HYD_PRESET, HYD_SEARCH, mongosh, seedReadyBbox } from './helpers'

/** TC-E-01..07 (docs/04 §5) — browser E2E over the real local stack. */

interface Call {
  method: string
  path: string
  status?: number
}

function track(page: Page): Call[] {
  const calls: Call[] = []
  page.on('response', (response) => {
    const request = response.request()
    if (request.resourceType() !== 'fetch' && request.resourceType() !== 'xhr') return
    calls.push({
      method: request.method(),
      path: new URL(response.url()).pathname,
      status: response.status(),
    })
  })
  return calls
}

test('TC-E-01 cold-start happy path: preset → ready → headline + charts', async ({ page }) => {
  const calls = track(page)
  await page.goto('/')

  // defaults come from the server registry (CF-01)
  await expect(page.locator('#hazard-select')).toHaveValue('heatwave')
  await expect(page.locator('#preset-select')).toBeEnabled()

  await page.selectOption('#preset-select', 'city_hyderabad')

  await expect(page.getByTestId('summary-card')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByTestId('headline')).toContainText('Heatwaves')
  await expect(page.locator('canvas')).toBeVisible()
  await expect(page.getByTestId('grid-caveat')).toHaveCount(0)
  await expect(page.getByTestId('empty-state')).toHaveCount(0)
  await expect(page.url()).toContain(`region_id=${HYD_PRESET}`)

  // request order: config → presets → region create → analysis
  const index = (match: (call: Call) => boolean) => calls.findIndex(match)
  const hazards = index((call) => call.path.endsWith('/api/v1/hazards'))
  const presets = index((call) => call.path.endsWith('/api/v1/regions/presets'))
  const create = index((call) => call.method === 'POST' && call.path.endsWith('/api/v1/regions'))
  const summary = index((call) => call.path.endsWith('/api/v1/analysis/summary'))
  expect(hazards).toBeGreaterThanOrEqual(0)
  expect(presets).toBeGreaterThan(hazards)
  expect(create).toBeGreaterThan(presets)
  expect(summary).toBeGreaterThan(create)
})

test('TC-E-02 custom bbox: form posts kind=bbox → honest preparing state', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('#preset-select')).toBeEnabled()

  await page.fill('#bbox-min-lat', '17')
  await page.fill('#bbox-max-lat', '18')
  await page.fill('#bbox-min-lon', '78')
  await page.fill('#bbox-max-lon', '79')
  await page.fill('#bbox-name', 'E2E box')

  const [response] = await Promise.all([
    page.waitForResponse(
      (candidate) =>
        candidate.request().method() === 'POST' &&
        new URL(candidate.url()).pathname.endsWith('/api/v1/regions'),
    ),
    page.getByRole('button', { name: 'Analyze this box' }).click(),
  ])
  expect([200, 201]).toContain(response.status())
  const requestBody = response.request().postDataJSON() as { kind: string }
  expect(requestBody.kind).toBe('bbox')

  // provider quota (EC-31) can't finish a real backfill → the FE must show
  // the honest preparing state while the region is backfilling (CF-02 step 5)
  const created = (await response.json()) as { region_id: string }
  await expect(page.getByTestId('preparing-card')).toContainText('E2E box')
  await expect(page.url()).toContain(`region_id=${created.region_id}`)

  try {
    // CF-02 postcondition once a bbox region is ready (seeded offline):
    // the areal-mean footer is rendered and charts come up.
    seedReadyBbox()
    await page.goto(`/?region_id=${BBOX_REGION}&start_year=1990&end_year=2020`)
    await expect(page.getByTestId('grid-caveat')).toHaveText(
      'Based on a 3×3 grid average across the selected box',
    )
    await expect(page.locator('canvas')).toBeVisible()
    await expect(page.getByTestId('summary-card')).toBeVisible()
  } finally {
    // leave the created box out of the sweeper's requeue loop
    mongosh(`db.regions.updateOne({_id: "${created.region_id}"}, {$set: {status: "failed"}})`)
  }
})

test('TC-E-03 range change refetches without a duplicate region POST', async ({ page }) => {
  const calls = track(page)
  await page.goto(`/?region_id=${HYD_PRESET}`)
  await expect(page.getByTestId('summary-card')).toBeVisible({ timeout: 30_000 })
  const createsBefore = calls.filter(
    (call) => call.method === 'POST' && call.path.endsWith('/api/v1/regions'),
  ).length

  await page.fill('#start-year', '2000')

  await expect(page).toHaveURL(/start_year=2000/)
  await expect(page.getByTestId('chart-summary')).toContainText('2000–2020', {
    timeout: 30_000,
  })

  const createsAfter = calls.filter(
    (call) => call.method === 'POST' && call.path.endsWith('/api/v1/regions'),
  ).length
  expect(createsAfter).toBe(createsBefore)

  const ranged = calls.filter((call) => call.path.endsWith('/api/v1/analysis/summary'))
  expect(ranged.length).toBeGreaterThan(1)
  expect(ranged.slice(-1)[0].status).toBeGreaterThanOrEqual(200)
  expect(ranged.slice(-1)[0].status).toBeLessThan(400) // 200 or ETag 304
})

test('TC-E-04 chart metric toggle re-renders and updates a11y text', async ({ page }) => {
  await page.goto(`/?region_id=${HYD_PRESET}`)
  await expect(page.getByTestId('chart-summary')).toContainText('Heatwave days per year', {
    timeout: 30_000,
  })

  const group = page.getByRole('group', { name: 'Chart metric' })
  const events = group.getByRole('button', { name: 'Events' })
  const days = group.getByRole('button', { name: 'Days' })
  await expect(events).toHaveAttribute('aria-pressed', 'false')

  await events.click()
  await expect(events).toHaveAttribute('aria-pressed', 'true')
  await expect(days).toHaveAttribute('aria-pressed', 'false')
  await expect(page.getByTestId('chart-summary')).toContainText('Events per year')
  await expect(page.locator('canvas')).toBeVisible()
})

test('TC-E-05 zero-event range renders the honest EmptyState (CF-07)', async ({ page }) => {
  await page.goto(`/?region_id=${HYD_SEARCH}&start_year=1950&end_year=1951`)

  await expect(page.getByTestId('empty-state')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByTestId('empty-state')).toContainText('An empty result is a result')
  await expect(page.getByTestId('empty-state')).toContainText(
    'heatwave events between 1950 and 1951',
  )
  await expect(page.getByTestId('summary-card')).toHaveCount(0)
  await expect(page.locator('canvas')).toBeVisible() // zero chart, still honest
})

test('TC-E-06 low coverage suppresses the trend claim (CF-12)', async ({ page }) => {
  const years = '{ $gte: 2001, $lte: 2015 }'
  mongosh(`
db.getCollection("_e2e_backup_e06").deleteMany({})
db.getCollection("_e2e_backup_e06").insertMany(db.hazard_annual_stats.find(
  {region_id: "${HYD_SEARCH}", hazard: "heatwave", year: ${years}}).toArray())
db.hazard_annual_stats.updateMany(
  {region_id: "${HYD_SEARCH}", hazard: "heatwave", year: ${years}},
  {$set: {coverage_pct: 50}})
`)
  try {
    // fresh range → no L2 hit from earlier tests
    await page.goto(`/?region_id=${HYD_SEARCH}&start_year=1990&end_year=2021`)
    await expect(page.getByTestId('data-quality')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('data-quality')).toContainText('overclaim')
    await expect(page.getByTestId('summary-card')).toHaveCount(0) // no trend claim
    await expect(page.getByTestId('headline')).toHaveCount(0)
  } finally {
    mongosh(`
const backup = db.getCollection("_e2e_backup_e06").find({}).toArray()
backup.forEach((d) => db.hazard_annual_stats.updateOne({_id: d._id}, {$set: {coverage_pct: d.coverage_pct}}))
db.getCollection("_e2e_backup_e06").deleteMany({})
`)
  }
})

test('TC-E-07 invalid URL params: blocked client-side with Reset, app not blank', async ({
  page,
}) => {
  const calls = track(page)
  await page.goto(`/?region_id=${HYD_PRESET}&start_year=2021&end_year=1990`)

  await expect(page.getByTestId('error-card')).toBeVisible()
  await expect(page.getByTestId('error-card')).toContainText('Invalid analysis parameters')
  await expect(page.getByTestId('error-fields')).toContainText('start_year must be <= end_year')
  await expect(page.locator('#hazard-select')).toBeVisible() // app is not blank

  const analysisCalls = calls.filter((call) => call.path.includes('/api/v1/analysis/'))
  expect(analysisCalls).toHaveLength(0)

  await page.getByRole('button', { name: 'Reset to defaults' }).click()
  await expect(page.getByTestId('empty-prompt')).toBeVisible()
  await expect(page.locator('#hazard-select')).toHaveValue('heatwave')
  await expect(page).not.toHaveURL(/region_id=/)
})
