/**
 * The batch screen (BATCH-3..8, UX-10, TC-20).
 *
 * These assert the properties an agent depends on, not the markup that happens to
 * express them today:
 *
 *   - Results are readable WHILE the job runs. Gating the table on completion turns a
 *     multi-minute batch into a spinner, which is the behaviour Janet already has.
 *   - The order on screen is the server's `worst_first`. If this screen re-sorted, the
 *     ordering and the recommendation could disagree, and an agent who catches that once
 *     stops trusting the ordering entirely.
 *   - Filters HIDE rows. They never reorder them and they never change the counts.
 *   - A row that failed is never dressed as a finding against the label. "We could not
 *     check this" and "this label is wrong" are different facts and the second one is a
 *     false accusation.
 *   - Bad manifest rows are reported by row number and do not reject the good ones.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import BatchCheck, { formatEta, percentDone } from './BatchCheck';
import { aggregate, fieldResult } from '../testing';
import type { Application, BatchItem, BatchStatus, VerificationResult } from '../types';

const APPLICATION: Application = {
  commodity: 'spirits',
  brand_name: 'Old Tom',
  class_type: 'Gin',
  alcohol_content: 45,
  net_contents: '750 mL',
  producer_name: 'Old Tom Distillery',
  producer_address: 'Louisville, KY',
  country_of_origin: null,
  is_import: false,
};

function result(recommendation: VerificationResult['aggregate']['recommendation']): VerificationResult {
  return {
    request_id: 'req_1',
    aggregate: aggregate({ recommendation, driving_field: 'government_warning' }),
    fields: [fieldResult('government_warning', recommendation === 'ready_to_approve' ? 'match' : 'mismatch')],
    images: [],
    timings_ms: { ingest: 1, quality: 1, preprocess: 1, extract: 1, compare: 1, adjudicate: null, total: 5 },
    cost: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0, usd: 0 },
  };
}

function item(overrides: Partial<BatchItem> & { item_id: string; row: number }): BatchItem {
  return {
    job_id: 'job_1',
    state: 'done',
    attempts: 1,
    application: APPLICATION,
    images: ['front.png'],
    result: null,
    failure: null,
    decisions: {},
    created_at: 0,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

function status(overrides: Partial<BatchStatus> = {}): BatchStatus {
  return {
    job_id: 'job_1',
    state: 'processing',
    counts: { total: 3, queued: 0, processing: 0, done: 3, failed: 0 },
    eta_seconds: null,
    summary: { by_recommendation: {}, by_verdict: {}, worst_first: [], headline: '' },
    items: [],
    cost: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0, usd: 0 },
    row_errors: [],
    unmatched_files: [],
    expires_at: 0,
    message: '',
    ...overrides,
  };
}

/** Drive the screen to its results state without going through the file picker. */
async function startBatch(
  first: BatchStatus,
  accepted: Partial<Record<string, unknown>> = {},
  /** Answer a request the default mock does not know about. Return null to fall through. */
  extra?: (url: string, init?: RequestInit) => Promise<Response | null>,
) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (extra) {
      const answered = await extra(url, init);
      if (answered) return answered;
    }
    if (url === '/batch') {
      return new Response(
        JSON.stringify({ job_id: 'job_1', accepted: first.counts.total, message: 'Queued.', ...accepted }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      );
    }
    return new Response(JSON.stringify(first), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);

  const user = userEvent.setup();
  render(<BatchCheck />);

  const manifest = new File(['row,brand\n1,Old Tom\n'], 'manifest.csv', { type: 'text/csv' });
  await user.upload(screen.getByLabelText(/spreadsheet/i), manifest);
  await user.click(screen.getByRole('button', { name: /start checking/i }));

  return { user, fetchMock };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('progress wording', () => {
  it('says how long in words rather than in a stopwatch', () => {
    expect(formatEta(null)).toMatch(/working out/i);
    expect(formatEta(0)).toMatch(/almost/i);
    expect(formatEta(30)).toMatch(/second/i);
    expect(formatEta(180)).toBe('About 3 minutes left.');
    expect(formatEta(60)).toBe('About 1 minute left.');
  });

  it('counts failures as finished, because they are', () => {
    // A batch of 10 with 8 done and 2 failed is finished, not 80% done. Leaving failures
    // out of the bar means it never reaches 100% and the agent waits for nothing.
    expect(percentDone(8, 2, 10)).toBe(100);
    expect(percentDone(0, 0, 0)).toBe(0);
  });
});

describe('the triage table', () => {
  it('shows finished rows while the job is still running', async () => {
    // The property: a running job with results shows them. Not "after done".
    await startBatch(
      status({
        state: 'processing',
        counts: { total: 3, queued: 1, processing: 1, done: 1, failed: 0 },
        items: [item({ item_id: 'a', row: 2, result: result('return_for_correction') })],
      }),
    );

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    // Scoped to the table: the filter buttons carry these words too, and matching one of
    // those would let this test pass with an empty table.
    const table = within(screen.getByRole('table'));
    expect(table.getByText('Return for correction')).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toBeInTheDocument();
  });

  it('renders rows in the order the server ranked them, not the order they arrived', async () => {
    await startBatch(
      status({
        state: 'done',
        items: [
          item({ item_id: 'clean', row: 1, result: result('ready_to_approve') }),
          item({ item_id: 'bad', row: 2, result: result('return_for_correction') }),
        ],
        // Server says the rejection comes first. The array above says otherwise.
        summary: { by_recommendation: {}, by_verdict: {}, worst_first: ['bad', 'clean'], headline: '' },
      }),
    );

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    const rows = screen.getAllByRole('row').slice(1); // drop the header
    expect(within(rows[0] as HTMLElement).getByText('2')).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText('1')).toBeInTheDocument();
  });

  it('keeps rows the server did not rank instead of dropping them', async () => {
    // worst_first is the ordering, not the guest list. An item missing from it must still
    // appear — silently losing a row from a compliance queue is the worst failure here.
    await startBatch(
      status({
        state: 'done',
        items: [
          item({ item_id: 'ranked', row: 1, result: result('needs_review') }),
          item({ item_id: 'unranked', row: 9, result: result('ready_to_approve') }),
        ],
        summary: { by_recommendation: {}, by_verdict: {}, worst_first: ['ranked'], headline: '' },
      }),
    );

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    expect(screen.getByText('9')).toBeInTheDocument();
  });

  it('filters hide rows without reordering the ones that remain', async () => {
    const { user } = await startBatch(
      status({
        state: 'done',
        counts: { total: 3, queued: 0, processing: 0, done: 3, failed: 0 },
        items: [
          item({ item_id: 'bad', row: 1, result: result('return_for_correction') }),
          item({ item_id: 'mid', row: 2, result: result('needs_review') }),
          item({ item_id: 'ok', row: 3, result: result('ready_to_approve') }),
        ],
        summary: { by_recommendation: {}, by_verdict: {}, worst_first: ['bad', 'mid', 'ok'], headline: '' },
      }),
    );

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    expect(screen.getAllByRole('row')).toHaveLength(4);

    await user.click(screen.getByRole('button', { name: /^Needs review/ }));
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows).toHaveLength(1);
    expect(within(rows[0] as HTMLElement).getByText('2')).toBeInTheDocument();
  });

  it('never presents a failed item as a finding against the label', async () => {
    // "We could not check this" is an action for the agent. "This label is wrong" is an
    // accusation. Collapsing the first into the second is a false finding.
    const { user } = await startBatch(
      status({
        state: 'done',
        counts: { total: 1, queued: 0, processing: 0, done: 0, failed: 1 },
        items: [
          item({
            item_id: 'boom',
            row: 4,
            state: 'failed',
            result: null,
            failure: { code: 'provider_unavailable', message: 'The checking service did not answer.', next_step: 'retry', attempts: 2 },
          }),
        ],
        summary: { by_recommendation: {}, by_verdict: {}, worst_first: ['boom'], headline: '' },
      }),
    );

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    const table = within(screen.getByRole('table'));
    expect(table.queryByText('Return for correction')).not.toBeInTheDocument();
    expect(table.queryByText('Mismatch')).not.toBeInTheDocument();
    expect(table.getByText(/could not check/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /open/i }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText(/was not checked/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/nothing here is a finding/i)).toBeInTheDocument();
  });

  it('says what the leading number is, because worst-first makes it look scrambled', async () => {
    // The table sorts by seriousness, so the row numbers come out 6, 2 — correct, and
    // indistinguishable from a shuffled table if nothing on screen says what they are.
    // "Row" alone did not: it is the one column whose meaning lives in a file the reviewer
    // is not looking at.
    await startBatch(
      status({
        state: 'done',
        counts: { total: 2, queued: 0, processing: 0, done: 2, failed: 0 },
        items: [
          item({ item_id: 'ok', row: 2, result: result('ready_to_approve') }),
          item({ item_id: 'bad', row: 6, result: result('return_for_correction') }),
        ],
        summary: { by_recommendation: {}, by_verdict: {}, worst_first: ['bad', 'ok'], headline: '' },
      }),
    );

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    expect(
      screen.getByRole('columnheader', { name: /manifest row/i }),
    ).toBeInTheDocument();

    // And the explanation is readable, not hidden from sighted readers — the question it
    // answers is asked by everyone looking at the column.
    const caption = screen.getByText(/first application is row 2/i);
    expect(caption).toBeVisible();
    expect(caption).not.toHaveClass('visually-hidden');
    expect(caption).toHaveTextContent(/most serious first/i);
  });

  it('names the field that drove the recommendation, not only its verdict', async () => {
    // "Missing" is true of a label with no alcohol content and of an import with no
    // country of origin, and the server's sentence for both is "a required element is not
    // on the label". Without the field name those two rows are the same row on screen.
    const missing = result('return_for_correction');
    await startBatch(
      status({
        state: 'done',
        counts: { total: 1, queued: 0, processing: 0, done: 1, failed: 0 },
        items: [
          item({
            item_id: 'origin',
            row: 5,
            result: {
              ...missing,
              aggregate: aggregate({
                recommendation: 'return_for_correction',
                driving_field: 'country_of_origin',
                rationale: 'A required element is not on the label.',
              }),
              fields: [fieldResult('country_of_origin', 'missing')],
            },
          }),
        ],
        summary: { by_recommendation: {}, by_verdict: {}, worst_first: ['origin'], headline: '' },
      }),
    );

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    const cell = within(screen.getByRole('table')).getByText(/country of origin/i);
    expect(cell).toHaveTextContent(/missing/i);
  });

  it('says why a clean row is clean instead of leaving the cell empty', async () => {
    // A row where nothing needed attention has no driving field, so this cell used to be a
    // dash sitting next to rows carrying a whole sentence — which reads as a value that
    // failed to load rather than as an answer.
    const clean = result('ready_to_approve');
    await startBatch(
      status({
        state: 'done',
        counts: { total: 1, queued: 0, processing: 0, done: 1, failed: 0 },
        items: [
          item({
            item_id: 'ok',
            row: 2,
            result: {
              ...clean,
              aggregate: aggregate({
                recommendation: 'ready_to_approve',
                driving_field: null,
                rationale: 'Every required field on the label matches the application.',
              }),
              fields: [fieldResult('government_warning', 'match')],
            },
          }),
        ],
        summary: { by_recommendation: {}, by_verdict: {}, worst_first: ['ok'], headline: '' },
      }),
    );

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    const table = within(screen.getByRole('table'));
    expect(table.getByText(/nothing needed attention/i)).toBeInTheDocument();
    expect(table.queryByText('—')).not.toBeInTheDocument();
  });

  it('gives the reason when a finished row has no single field to blame', async () => {
    // The unreadable photograph: every field came back Unreadable, so nothing is "the"
    // driving field, and the reason lives in the aggregate. A dash here would hide the one
    // sentence that tells the agent what to do about the row.
    const blurred = result('needs_review');
    await startBatch(
      status({
        state: 'done',
        counts: { total: 1, queued: 0, processing: 0, done: 1, failed: 0 },
        items: [
          item({
            item_id: 'blur',
            row: 7,
            result: {
              ...blurred,
              aggregate: aggregate({
                recommendation: 'needs_review',
                driving_field: null,
                rationale: 'The photo is too blurry to read the label. Retake it.',
              }),
              fields: [fieldResult('government_warning', 'unreadable')],
            },
          }),
        ],
        summary: { by_recommendation: {}, by_verdict: {}, worst_first: ['blur'], headline: '' },
      }),
    );

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    const table = within(screen.getByRole('table'));
    expect(table.getByText(/too blurry to read the label/i)).toBeInTheDocument();
    expect(table.queryByText(/nothing needed attention/i)).not.toBeInTheDocument();
  });

  it('offers a retry only when something actually failed', async () => {
    await startBatch(
      status({
        state: 'done',
        counts: { total: 1, queued: 0, processing: 0, done: 1, failed: 0 },
        items: [item({ item_id: 'ok', row: 1, result: result('ready_to_approve') })],
        summary: { by_recommendation: {}, by_verdict: {}, worst_first: ['ok'], headline: '' },
      }),
    );

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });
});

/**
 * Drive the screen through the sample button rather than the file picker. Same helper
 * shape as `startBatch`, and deliberately so — the assertion that matters is that the
 * screen cannot tell the difference afterwards.
 */
async function startSample(first: BatchStatus, accepted: Record<string, unknown> = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === '/batch/sample') {
      return new Response(
        JSON.stringify({
          job_id: 'job_1',
          accepted: first.counts.total,
          message: 'Queued.',
          ...accepted,
        }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      );
    }
    return new Response(JSON.stringify(first), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);

  const user = userEvent.setup();
  render(<BatchCheck />);
  await user.click(screen.getByRole('button', { name: /try a sample batch/i }));
  return { user, fetchMock };
}

describe('the sample batch', () => {
  it('runs through the same queue and polling as an uploaded one', async () => {
    const { fetchMock } = await startSample(
      status({
        state: 'processing',
        counts: { total: 7, queued: 6, processing: 1, done: 0, failed: 0 },
        items: [item({ item_id: 'a', row: 2, result: result('return_for_correction') })],
        summary: { by_recommendation: {}, by_verdict: {}, worst_first: ['a'], headline: '' },
      }),
      { accepted: 7 },
    );

    // The running screen, its progress bar, and a poll — not a canned table.
    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    expect(screen.getByRole('progressbar')).toBeInTheDocument();
    expect(fetchMock.mock.calls.map((call) => String(call[0]))).toContain('/batch/sample');
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).startsWith('/batch/job_1?')),
      ).toBe(true),
    );
  });

  it('does not blame the reviewer for the row it broke on purpose', async () => {
    // The sample ships one malformed row so a reviewer meets a bad row here rather than
    // in their own file. "Fix these rows and upload them again" would send them looking
    // for a spreadsheet they never had.
    await startSample(
      status({
        state: 'processing',
        counts: { total: 7, queued: 7, processing: 0, done: 0, failed: 0 },
        row_errors: [{ row: 9, column: 'commodity', message: 'Not a commodity.' }],
      }),
      { accepted: 7, row_errors: [{ row: 9, column: 'commodity', message: 'Not a commodity.' }] },
    );

    await waitFor(() =>
      expect(screen.getByText(/the sample includes 1 row that cannot be used/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/nothing here is your mistake/i)).toBeInTheDocument();
    expect(screen.queryByText(/fix these rows/i)).not.toBeInTheDocument();
    // Still reported by row number — the demonstration is worthless if it hides the fact.
    expect(screen.getByText('9')).toBeInTheDocument();
  });
});

describe('the counts above the table', () => {
  it('does not print the same total twice on a batch that finished cleanly', async () => {
    // "Applications 3 / Checked 3" is one fact read out twice, on the screen whose whole
    // argument is that it never claims more than it knows. A reader who learns one pair of
    // numbers means nothing reads the next pair less carefully.
    await startBatch(
      status({
        state: 'done',
        counts: { total: 3, queued: 0, processing: 0, done: 3, failed: 0 },
        items: [item({ item_id: 'ok', row: 2, result: result('ready_to_approve') })],
        summary: { by_recommendation: {}, by_verdict: {}, worst_first: ['ok'], headline: '' },
      }),
    );

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    expect(screen.getByText('Applications')).toBeInTheDocument();
    expect(screen.queryByText('Checked')).not.toBeInTheDocument();
  });

  it('keeps the two apart while they still say different things', async () => {
    // Mid-run, and on a finished job that lost rows, they are not the same number and each
    // one is worth reading.
    await startBatch(
      status({
        state: 'processing',
        counts: { total: 3, queued: 1, processing: 0, done: 2, failed: 0 },
        items: [item({ item_id: 'ok', row: 2, result: result('ready_to_approve') })],
        summary: { by_recommendation: {}, by_verdict: {}, worst_first: ['ok'], headline: '' },
      }),
    );

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    const counts = screen.getByText('Checked').closest('.batch__count');
    expect(counts).toHaveTextContent('2');
    expect(screen.getByText('Applications').closest('.batch__count')).toHaveTextContent('3');
  });
});

describe('decisions on a finished job', () => {
  /**
   * The state almost all triage happens in, and the one where this was broken.
   *
   * A finished job stops polling — correctly, since it cannot change — so nothing
   * refreshes `item.decisions` again. With the ruling held inside the dialog it went out
   * of existence the moment the dialog closed, and reopening the row showed an un-pressed
   * button. It demoed fine only because a RUNNING job's polls were papering over it.
   */
  it('keeps a ruling across closing and reopening the row', async () => {
    const done = status({
      state: 'done',
      counts: { total: 1, queued: 0, processing: 0, done: 1, failed: 0 },
      items: [item({ item_id: 'a', row: 2, result: result('return_for_correction') })],
      summary: { by_recommendation: {}, by_verdict: {}, worst_first: ['a'], headline: '' },
    });

    const { user } = await startBatch(done, {}, async (url, init) => {
      if (!url.endsWith('/decisions')) return null;
      const body = JSON.parse(String(init?.body ?? '{}'));
      // The server echoes the item back with the ruling recorded — and then never
      // speaks again, because the job is done and polling has stopped.
      return new Response(
        JSON.stringify({ ...done.items[0], decisions: body.decisions }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      );
    });

    await waitFor(() => expect(screen.getByRole('table')).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: /open row 2/i }));

    // Attention rows open themselves, so the decision buttons are already on screen.
    const agree = within(screen.getByRole('dialog')).getByRole('button', { name: 'I agree' });
    await user.click(agree);
    await waitFor(() => expect(agree).toHaveAttribute('aria-pressed', 'true'));

    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /close/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: /open row 2/i }));
    expect(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'I agree' }),
    ).toHaveAttribute('aria-pressed', 'true');
  });
});

describe('a manifest with bad rows (TC-20)', () => {
  it('reports them by row number and still queues the good ones', async () => {
    await startBatch(
      status({
        state: 'processing',
        counts: { total: 2, queued: 2, processing: 0, done: 0, failed: 0 },
        row_errors: [{ row: 7, column: 'alcohol_content', message: 'Not a number.' }],
      }),
      { accepted: 2, row_errors: [{ row: 7, column: 'alcohol_content', message: 'Not a number.' }] },
    );

    await waitFor(() =>
      expect(screen.getByText(/1 row in the spreadsheet could not be used/i)).toBeInTheDocument(),
    );
    expect(screen.getByText('7')).toBeInTheDocument();
    expect(screen.getByText('alcohol_content')).toBeInTheDocument();
    // The good rows were not held hostage by the bad one.
    expect(screen.getByText(/everything else was queued/i)).toBeInTheDocument();
    // And it says where row 7 went. The notice names a row number, the table below is a
    // list of row numbers, and the one named here is precisely the one absent from it —
    // so without this sentence the notice reads as a row that went missing in transit.
    expect(screen.getByText(/does not appear in the results below/i)).toBeInTheDocument();
    // The two tables number their rows the same way and say so with the same words.
    expect(
      screen.getAllByRole('columnheader', { name: /manifest row/i }).length,
    ).toBeGreaterThan(0);
  });
});
