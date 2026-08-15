/**
 * The batch flow, end to end, in a real browser against a real server (ENG-2).
 *
 * ## How to run it
 *
 *     npm --prefix web run build
 *     LABELPROOF_FAKE_PROVIDER=1 LABELPROOF_BATCH_WORKERS=1 \
 *       .venv/bin/uvicorn api.main:app --port 8000
 *     npm --prefix web run e2e -- --project=batch
 *
 * FastAPI serves `web/dist` itself, so this drives one origin, exactly as production
 * does. Point somewhere else with `LABELPROOF_E2E_URL`.
 *
 * `LABELPROOF_BATCH_WORKERS=1` is not decoration. Six fixture items across six threads
 * can finish inside a single 1.5s poll, and then "results appear while the job runs" —
 * the property BATCH-5 exists for — has no window in which to be observed. One worker
 * makes the run sequential and the streaming visible. A `beforeAll` check refuses to run
 * without it rather than letting the assertion quietly become a coin flip.
 *
 * ## What this must never run against
 *
 * **`dev/mockApi.ts`.** It does not implement `/batch` at all, and its `/verify` hands
 * back one canned verdict after a hardcoded 2.2s sleep. This project has already spent
 * hours testing that stand-in and reading the results as facts about the product. The
 * guard below refuses to start unless `/health` answers as the real API in fixture mode,
 * so the failure is one loud sentence instead of a suite that passes against nothing.
 *
 * **The deployment.** `fly.toml` pins `LABELPROOF_FAKE_PROVIDER=0`, so pointing this at
 * production spends real model calls on six labels to test a queue. The same guard stops
 * it: `simulated` comes back false and the run refuses.
 *
 * ## What it does not prove
 *
 * That the verdicts are correct. The fixture provider replays recorded readings, so this
 * asserts the batch machinery — queue, stream, order, drill-in, decision, export — carries
 * whatever the pipeline produced. Whether the pipeline is right is `tests/` and the golden
 * set, and conflating the two would let a broken model pass a green e2e run.
 */

import { expect, test, type Browser, type Page } from '@playwright/test';

/** Worst first. The ladder the server ranks by, mirrored here only to check the order. */
const RANK: Record<string, number> = {
  return_for_correction: 0,
  needs_review: 1,
  ready_to_approve: 2,
};

interface PollObservation {
  state: string;
  items: number;
  withResult: number;
}

let page: Page;
const polls: PollObservation[] = [];
/** True once the table has been seen listing rows while the progress bar was still up. */
let sawRowsWhileRunning = false;
let jobFinished = false;

async function openPage(browser: Browser): Promise<Page> {
  const baseURL = test.info().project.use.baseURL;
  expect(baseURL, 'the batch project has no baseURL').toBeTruthy();
  return browser.newPage({ baseURL });
}

/**
 * Refuse to run against anything but a real server in fixture mode.
 *
 * Three different wrong targets produce three different failures here, and each one is
 * named, because "the batch spec failed" against the mock looks identical to a broken
 * feature and costs an afternoon.
 */
async function assertRealBackend(target: Page): Promise<void> {
  const response = await target.request.get('/health');
  expect(
    response.ok(),
    `/health did not answer at ${test.info().project.use.baseURL}. Start the API first — ` +
      'see the header of this file. If you pointed this at `npm run dev`, that is ' +
      'dev/mockApi.ts, which has no /batch and must never be what this suite measures.',
  ).toBe(true);

  let body: Record<string, unknown>;
  try {
    body = (await response.json()) as Record<string, unknown>;
  } catch {
    throw new Error(
      '/health returned something that is not JSON — almost certainly the SPA shell from ' +
        'a Vite dev server backed by dev/mockApi.ts. Run the real API instead.',
    );
  }

  expect(
    body['simulated'],
    'the server is answering with a live provider. This spec queues six labels and is ' +
      'about the queue, not the model: run it with LABELPROOF_FAKE_PROVIDER=1.',
  ).toBe(true);
}

test.describe.configure({ mode: 'serial' });

test.describe('the batch flow, on a real server', () => {
  test.beforeAll(async ({ browser }) => {
    page = await openPage(browser);
    await assertRealBackend(page);

    // Purely observational — nothing is intercepted or rewritten. The point of this spec
    // is that the bytes are the server's.
    page.on('response', (response) => {
      // GET only. `POST /batch/sample` has the same path shape and answers with the
      // acceptance envelope, which has no `state` — counting it would let the queue
      // response alone satisfy "something was seen mid-flight".
      if (response.request().method() !== 'GET') return;
      const url = new URL(response.url());
      if (!/^\/batch\/[^/]+$/.test(url.pathname)) return;
      void response
        .json()
        .then((body: Record<string, unknown>) => {
          const items = Array.isArray(body['items']) ? (body['items'] as unknown[]) : [];
          polls.push({
            state: String(body['state'] ?? ''),
            items: items.length,
            withResult: items.filter(
              (item) => (item as Record<string, unknown>)['result'] != null,
            ).length,
          });
        })
        .catch(() => undefined);
    });

    await page.goto('/batch-check');
    await page.getByRole('button', { name: /try a sample batch/i }).click();

    // Deterministic: the screen enters its running state from the queue response, before
    // any poll has come back. If this is not here, nothing was queued.
    await expect(page.getByRole('heading', { name: /checking your batch/i })).toBeVisible();
    await expect(page.getByRole('progressbar')).toBeVisible();

    // Watch until the job reports finished, recording whether the table ever listed rows
    // while the bar was still on screen. Polling the DOM rather than waiting on a single
    // selector, because the interesting state is a COINCIDENCE of two things and either
    // one alone proves nothing.
    const deadline = Date.now() + 150_000;
    while (Date.now() < deadline) {
      const seen = await page.evaluate(() => ({
        running: document.querySelector('[role="progressbar"]') !== null,
        rows: document.querySelectorAll('table.triage tbody tr').length,
        finished: /batch finished/i.test(
          document.querySelector('.batch__title')?.textContent ?? '',
        ),
      }));
      if (seen.running && seen.rows > 0) sawRowsWhileRunning = true;
      if (seen.finished) {
        jobFinished = true;
        break;
      }
      await page.waitForTimeout(120);
    }
  });

  test.afterAll(async () => {
    await page?.close();
  });

  test('the sample batch finishes', async () => {
    expect(jobFinished, 'the batch never reached its finished state').toBe(true);
    await expect(page.getByRole('heading', { name: /batch finished/i })).toBeVisible();
  });

  test('rows are readable while the job is still running (BATCH-5)', async () => {
    // The server's side of it: at least one status answered mid-flight and already
    // carried rows. If this fails with everything else green, the batch ran to completion
    // before the first poll — check LABELPROOF_BATCH_WORKERS=1 is set.
    const running = polls.filter((poll) => poll.state !== 'done');
    expect(
      running.length,
      'every status poll said "done" — the job finished before it could be watched',
    ).toBeGreaterThan(0);
    expect(
      running.some((poll) => poll.items > 0),
      'no running poll carried any rows, so the table had nothing to show early',
    ).toBe(true);

    // And the browser's side: the table was on screen next to the progress bar. Gating
    // the table on completion is what turns a multi-minute batch into a spinner.
    expect(sawRowsWhileRunning, 'the table only appeared after the job had finished').toBe(
      true,
    );
  });

  test('the sample says its broken row is deliberate, not the reviewer\'s', async () => {
    // The sample manifest ships one malformed row on purpose. Telling a reviewer to fix
    // it would blame them for a file they never uploaded.
    const notice = page.locator('.batch__notice[data-tone="serious"]');
    await expect(notice).toBeVisible();
    await expect(notice).toContainText(/sample includes/i);
    await expect(notice).not.toContainText(/fix these rows/i);
  });

  test('the order on screen is worst first (UX-10)', async () => {
    const ranks = await page.evaluate(() =>
      [...document.querySelectorAll('table.triage tbody tr')]
        .map((row) => row.getAttribute('data-recommendation'))
        .filter((value): value is string => Boolean(value)),
    );

    expect(ranks.length, 'no row carried a recommendation').toBeGreaterThan(1);
    // Non-decreasing severity rank. Rows that could not be checked carry no
    // recommendation and are skipped rather than guessed at — where the server files a
    // failure in the ranking is the server's call, not this test's.
    const ordered = ranks.map((name) => RANK[name] ?? Number.MAX_SAFE_INTEGER);
    expect(ordered, `worst-first broken: ${ranks.join(' → ')}`).toEqual(
      [...ordered].sort((a, b) => a - b),
    );
  });

  test('opening a row shows the label picture and its verdicts (HITL-3)', async () => {
    await openFirstResultRow();
    const dialog = page.getByRole('dialog');

    // The image is not merely present — the bytes arrived. A broken src renders an <img>
    // that satisfies every selector and shows the agent nothing.
    const picture = dialog.locator('img.evidence__image');
    await expect(picture).toBeVisible();
    await expect
      .poll(
        () => picture.evaluate((img: HTMLImageElement) => img.naturalWidth),
        { message: 'the label picture never decoded — the item image endpoint is not serving bytes' },
      )
      .toBeGreaterThan(0);

    // And the checklist beside it: field rows, each with a verdict word.
    await expect(dialog.locator('table.checklist tr.row').first()).toBeVisible();
    await expect(dialog.locator('[data-verdict]').first()).toBeVisible();

    await dialog.getByRole('button', { name: /^close$/i }).click();
    await expect(dialog).toBeHidden();
  });

  test('a recorded decision survives closing and reopening the row', async () => {
    const row = await openFirstResultRow();
    let dialog = page.getByRole('dialog');

    // The decision buttons live inside a row's reasoning panel.
    await dialog.getByRole('button', { name: /why this verdict/i }).first().click();
    const agree = dialog.getByRole('button', { name: 'I agree' }).first();
    await agree.click();

    // It must be SAVED, not merely pressed. The dialog springs the button back and shows
    // an alert if the write failed, so an alert here means this assertion would otherwise
    // have been testing optimism.
    await expect(agree).toHaveAttribute('aria-pressed', 'true');
    await expect(dialog.getByRole('alert')).toHaveCount(0);

    await dialog.getByRole('button', { name: /^close$/i }).click();
    await expect(dialog).toBeHidden();

    // Reopen the same application. The state has left the browser or it has not.
    await page.getByRole('button', { name: new RegExp(`open row ${row}$`, 'i') }).click();
    dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: /why this verdict/i }).first().click();
    await expect(dialog.getByRole('button', { name: 'I agree' }).first()).toHaveAttribute(
      'aria-pressed',
      'true',
    );

    await dialog.getByRole('button', { name: /^close$/i }).click();
  });

  test('the finished batch offers its export (BATCH-7)', async () => {
    const csv = page.getByRole('link', { name: /export csv/i });
    await expect(csv).toBeVisible();
    await expect(csv).toHaveAttribute('href', /\/batch\/.+\/export\.csv$/);
  });
});

/**
 * Open the first application that actually has a result, and return its row number.
 *
 * Not simply the first row: the sample's malformed entry and any item that could not be
 * checked have no verdicts and no picture, and opening one of those would make the two
 * tests below assert nothing while still passing.
 */
async function openFirstResultRow(): Promise<string> {
  const row = page.locator('table.triage tbody tr[data-recommendation]').first();
  await expect(row).toBeVisible();
  const number = (await row.locator('td').first().innerText()).trim();
  await row.getByRole('button', { name: /open/i }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  return number;
}
