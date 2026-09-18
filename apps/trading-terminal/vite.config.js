import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const TELEMETRY_ORIGIN = process.env.TELEMETRY_ORIGIN ?? 'http://127.0.0.1:8000'

// Both the REST snapshot and the telemetry socket are proxied, so the browser
// only ever talks to one origin and there is no CORS hop locally. A real
// deployment sets VITE_TELEMETRY_URL at build time instead.
const proxy = {
  '/api': { target: TELEMETRY_ORIGIN, changeOrigin: true },
  '/healthz': { target: TELEMETRY_ORIGIN, changeOrigin: true },
  '/ws': { target: TELEMETRY_ORIGIN, ws: true, changeOrigin: true },
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
  build: {
    target: 'es2022',
    chunkSizeWarningLimit: 700,
  },
})
