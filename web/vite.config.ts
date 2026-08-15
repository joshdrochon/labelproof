/// <reference types="vitest" />
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import type { Plugin } from 'vite';
import { mockApi } from './dev/mockApi';

/**
 * The API lives on the same origin in production — FastAPI serves this build as static
 * files (pinned build decision), so every request in `src/api.ts` is a plain relative path.
 *
 * In development there are two ways to satisfy those paths, and the choice is made by
 * the environment, never by anything in the interface:
 *
 *   LABELPROOF_API=http://127.0.0.1:8000 npm run dev   → proxy to the real API
 *   npm run dev                                        → dev-only stand-in (dev/mockApi)
 *
 * Neither reaches the production bundle.
 */
const API = process.env['LABELPROOF_API'];

const devApi: Plugin = {
  name: 'labelproof-dev-api',
  apply: 'serve',
  configureServer(server) {
    if (API) return;
    server.middlewares.use(mockApi);
  },
};

export default defineConfig({
  plugins: [react(), devApi],
  server: {
    port: 5173,
    proxy: API
      ? {
          '/verify': API,
          // The read-ahead call. Absent from this list it 404s against the dev server
          // while working perfectly against the API, which looks exactly like a broken
          // feature and is a missing line of proxy config.
          '/prepare': API,
          '/sample': API,
          '/batch': API,
          '/health': API,
          '/ready': API,
        }
      : undefined,
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
