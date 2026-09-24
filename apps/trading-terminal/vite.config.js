import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const TELEMETRY_ORIGIN = process.env.TELEMETRY_ORIGIN ?? 'http://127.0.0.1:8000'

// Both the REST snapshot and the telemetry socket are proxied, so the browser
// only ever talks to one origin and there is no CORS hop locally. A real
// deployment sets VITE_TELEMETRY_URL at build time instead.
//
// Note the socket is NOT proxied in production: Vercel rewrites cannot carry a
// WebSocket upgrade, so the browser has to reach the telemetry service directly.
const proxy = {
  '/api': { target: TELEMETRY_ORIGIN, changeOrigin: true },
  '/healthz': { target: TELEMETRY_ORIGIN, changeOrigin: true },
  '/ws': { target: TELEMETRY_ORIGIN, ws: true, changeOrigin: true },
}

/**
 * Check VITE_TELEMETRY_URL while building, not after deploying.
 *
 * Without it the bundle falls back to its own origin, so the dashboard ships
 * looking fine and can never connect. This turns that into a build log you
 * cannot miss, and into a hard failure for the cases that are certainly wrong.
 */
function validateTelemetryUrl() {
  return {
    name: 'validate-telemetry-url',
    apply: 'build',
    configResolved(config) {
      const raw = config.env.VITE_TELEMETRY_URL
      const onVercel = process.env.VERCEL === '1'
      const log = (message) => config.logger.warn(`\n[telemetry] ${message}\n`)

      if (!raw) {
        log(
          'VITE_TELEMETRY_URL is not set. The bundle will try to reach the telemetry\n' +
            '           service on its own origin, which only works behind the dev proxy.\n' +
            '           Set it to the origin of the FastAPI service before deploying.',
        )
        return
      }

      let url
      try {
        url = new URL(raw)
      } catch {
        throw new Error(
          `VITE_TELEMETRY_URL must be an absolute URL such as https://telemetry.example.com, got "${raw}".`,
        )
      }

      if (/^wss?:$/.test(url.protocol)) {
        const fixed = `${url.protocol === 'wss:' ? 'https' : 'http'}://${url.host}`
        throw new Error(
          `VITE_TELEMETRY_URL is "${raw}". Give the http(s) origin instead, "${fixed}": ` +
            'the app derives the wss socket URL from it and also needs it for the REST snapshot.',
        )
      }

      if (!/^https?:$/.test(url.protocol)) {
        throw new Error(`VITE_TELEMETRY_URL must use http or https, got "${url.protocol}".`)
      }

      // A Vercel deployment is always served over https, so an http origin here
      // is blocked as mixed content in every browser. Fail rather than ship it.
      if (onVercel && url.protocol === 'http:') {
        throw new Error(
          `VITE_TELEMETRY_URL is "${raw}", but Vercel serves this app over https and browsers ` +
            'block mixed content. Use an https origin so the socket can upgrade to wss.',
        )
      }

      // Only the origin is used, so anything after it would be dropped silently.
      if (url.pathname !== '/' || url.search || url.hash) {
        log(
          `VITE_TELEMETRY_URL is "${raw}", but only the origin (${url.origin}) is used.\n` +
            '           The path, query and fragment are ignored.',
        )
      }

      config.logger.info(`[telemetry] streaming from ${url.origin}`)
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), validateTelemetryUrl()],
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
  build: {
    target: 'es2022',
    chunkSizeWarningLimit: 700,
  },
})
