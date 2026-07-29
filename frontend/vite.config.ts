import { fileURLToPath, URL } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
// `vitest/config` re-exports `defineConfig` with the `test` block typed; the
// one from `vite` does not know about it.
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    // The API is called cross-origin on purpose, not through a dev proxy: the
    // backend's CORS + credentialed-cookie setup is then exercised in
    // development exactly as in production. A proxy would hide a misconfigured
    // `CORS_ORIGINS` until deploy day.
    port: 5173,
    strictPort: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
})
