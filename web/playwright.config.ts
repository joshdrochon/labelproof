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

/**
 * Where `batch.spec.ts` runs, and why it is not `BASE_URL`.
 *
 * The batch spec queues jobs and reads their results. Pointed at the deployment that
 * means real provider calls billed against a real key, at a per-run cost, for a test
 * whose whole subject is the queue rather than the model. Pointed at `npm run dev` with
 * no API it means `dev/mockApi.ts`, which does not implement `/batch` at all and answers
 * `/verify` with one canned verdict after a hardcoded 2.2s — hours have already gone into
 * testing that stand-in and mistaking it for the product.
 *
 * So it defaults to a LOCAL server with the fixture provider, and the spec refuses to run
 * against anything that does not identify itself as one. See `e2e/batch.spec.ts` for the
 * exact command.
 */
const BATCH_BASE_URL = process.env.LABELPROOF_E2E_URL ?? 'http://127.0.0.1:8000';

/** The batch spec, which every other project excludes and the batch project selects. */
const BATCH_SPEC = /batch\.spec\.ts/;

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
    {
      name: 'chromium',
      testIgnore: BATCH_SPEC,
      use: { ...devices['Desktop Chrome'] },
    },
    { name: 'firefox', testIgnore: BATCH_SPEC, use: { ...devices['Desktop Firefox'] } },
    // LP-315. A tablet viewport with touch, driven by Chromium.
    //
    // NOT `devices['iPad Pro 11']`, which is WebKit — and WebKit does not run on this
    // machine. macOS 14 pins Playwright to an older WebKit build than the 1.62 driver
    // expects, so every context fails at launch with an unknown protocol setting; the
    // 1.55 driver that matches has a build that will not download. Emulating the
    // viewport and touch on Chromium tests the LAYOUT and the TARGETS, which is what
    // LP-315 asks about, and it is honest that it does not test Safari's engine.
    {
      name: 'tablet',
      testIgnore: BATCH_SPEC,
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 834, height: 1112 },
        isMobile: false,
        hasTouch: true,
      },
    },
    /**
     * The batch flow, end to end, against a real server (ENG-2).
     *
     * One engine, not three: this asserts that a job queues, streams, orders and exports —
     * server behaviour the browser only reports on. Running it on the matrix would triple
     * the queued jobs to re-check Gecko's rendering of a table already covered by
     * `a11y.spec.ts`. A longer timeout because a batch is minutes of work, not one request.
     */
    {
      name: 'batch',
      testMatch: BATCH_SPEC,
      timeout: 180_000,
      use: { ...devices['Desktop Chrome'], baseURL: BATCH_BASE_URL },
    },
  ],

/*
 * SAFARI IS NOT COVERED, and pretending otherwise by deleting the project quietly would
 * be worse than saying so. An agency desktop is overwhelmingly Edge or Chrome, so the
 * engine gap is Chromium-vs-Gecko-vs-WebKit on the two that are covered; Safari remains
 * genuinely untested here.
 */
});
