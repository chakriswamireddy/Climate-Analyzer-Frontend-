import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

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
})
