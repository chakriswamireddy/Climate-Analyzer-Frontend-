import { defineConfig, devices } from '@playwright/test'

/**
 * Browser E2E (docs/04 §5, TC-E-01..07 + CF-05 poll UX) against the local
 * stack: `docker compose up -d` (API on :8000) must already be running; the
 * vite dev server (with its /api proxy) is started by Playwright.
 *
 * One worker: the tests share a live Mongo (they back up/restore stats and
 * promote one bbox region), so they must not interleave.
 */
export default defineConfig({
  testDir: './e2e',
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: true,
    timeout: 30_000,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
