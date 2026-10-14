import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// Live FE verification against a running backend: `npm run test:live`
// (start the API first: `docker compose up -d`, optionally `node scripts/cf-smoke.mjs`).
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.tsx'],
    include: ['src/test/live-render.test.tsx'],
    restoreMocks: true,
    unstubGlobals: true,
    testTimeout: 30_000,
  },
})
