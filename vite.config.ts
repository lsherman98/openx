import { defineConfig, loadEnv } from 'vite'
import viteReact from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

import { tanstackRouter } from '@tanstack/router-plugin/vite'
import { fileURLToPath, URL } from 'node:url'
import { sentrySourceMapOptions } from './scripts/sentry-sourcemaps.ts'

// https://vitejs.dev/config/
export default defineConfig(async ({ command, mode }) => {
  const upload = sentrySourceMapOptions(loadEnv(mode, process.cwd(), ''), command === 'build')
  const sentry = upload ? (await import('@sentry/vite-plugin')).sentryVitePlugin(upload) : undefined
  return {
  plugins: [
    tanstackRouter({ target: 'react', autoCodeSplitting: true }),
    viteReact(),
    tailwindcss(),
    sentry,
  ],

  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: { '/api': 'http://127.0.0.1:8090' },
  },
  build: { sourcemap: upload ? 'hidden' as const : false },
  }
})
