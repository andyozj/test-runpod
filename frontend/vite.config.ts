import { copyFileSync } from 'node:fs'
import { join } from 'node:path'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

/**
 * SPA fallback for the deployed stack: deploy/stack/serve.py mounts Starlette
 * StaticFiles(html=True), which serves `404.html` — not index.html — for an
 * unknown path. Without this a /jobs/:id deep link 404s instead of booting the
 * router. Vite dev and preview already fall back on their own.
 */
function spaFallback(): Plugin {
  return {
    name: 'spa-404-fallback',
    apply: 'build',
    // writeBundle, not generateBundle: vite's own html plugin finalizes
    // index.html after the bundle hook runs
    writeBundle(options) {
      const dir = options.dir ?? 'dist'
      copyFileSync(join(dir, 'index.html'), join(dir, '404.html'))
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), spaFallback()],
  server: {
    proxy: {
      '/v1': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
})
