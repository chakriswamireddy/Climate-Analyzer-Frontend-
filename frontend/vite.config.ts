import react from '@vitejs/plugin-react'
import { configDefaults, defineConfig } from 'vitest/config'

// Proxy backend API to the local FastAPI server (docs/05 §2 P0).
// In compose the api listens on :8000; keep the default target for host dev.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/healthz': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.tsx'],
    // unit suite only: live checks run via `npm run test:live`.
    exclude: [...configDefaults.exclude, 'src/test/live-render.test.tsx'],
    restoreMocks: true,
    unstubGlobals: true,
  },
})
