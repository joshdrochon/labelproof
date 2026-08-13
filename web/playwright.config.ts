/**
 * Browser-driven accessibility and cross-browser checks (LP-264, LP-265, LP-314, LP-315,
 * LP-316).
 *
 * These run against the DEPLOYED URL by default, not a dev server. The claim being
 * tested is "the thing a reviewer opens is keyboard-operable in Safari", and a Vite dev
 * server is not that thing — it serves unminified modules over a different origin with
 * different headers. Point at a local preview with BASE_URL when iterating.
 *
 * What these DO NOT prove: that a screen reader says something sensible. Playwright can
 * assert the accessibility tree Chromium exposes — names, roles, states — which is what
 * a screen reader consumes, but not what VoiceOver or NVDA actually announces from it.
 * That gap is real and is recorded in the README rather than papered over.
 */

import { defineConfig, devices } from '@playwright/test';

const BASE_URL = process.env.BASE_URL ?? 'https://labelproof.fly.dev';

export default defineConfig({
  testDir: './e2e',
  // Against a live deployment with a per-IP rate limit, parallel workers race each other
  // into 429s and the failure looks like a broken page.
  workers: 1,
  fullyParallel: false,
  reporter: [['list'], ['json', { outputFile: 'e2e-results.json' }]],
  timeout: 60_000,
  use: {
    baseURL: BASE_URL,
    // Every failure gets a screenshot and a trace. A cross-browser bug nobody can see is
    // a bug report nobody acts on.
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    // LP-315. iPad Pro portrait is the realistic tablet for an agent at a bench.
    { name: 'tablet', use: { ...devices['iPad Pro 11'] } },
  ],
});
