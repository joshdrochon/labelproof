/**
 * The batch drill-in (HITL-3, LP-174).
 *
 * These assert the two things an agent working a queue depends on, and nothing about the
 * markup that happens to express them today:
 *
 *   - **The label is on screen.** A verdict that says the brand name disagrees, with no
 *     way to look at the brand name, is the tool asking to be taken on trust. The picture
 *     and the numbered outlines are the citation.
 *   - **A failed item still says so.** No result means no regions and no picture, and the
 *     dialog must render its "could not check" state rather than falling over reaching
 *     into a result that is not there. That path is the one an agent hits on a bad day.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ItemDetail from './ItemDetail';
import { aggregate, fieldResult } from '../testing';
import type { Application, BatchItem, ImageReport, VerificationResult } from '../types';

const APPLICATION: Application = {
  commodity: 'spirits',
  brand_name: 'Old Tom',
  class_type: 'Kentucky Straight Bourbon Whiskey',
  alcohol_content: 45,
  net_contents: '750 mL',
  producer_name: 'Old Tom Distillery',
  producer_address: 'Bardstown, Kentucky',
  country_of_origin: null,
  is_import: false,
};

function imageReport(index: number, role: string | null): ImageReport {
  return {
    index,
    role,
    quality: {
      blur: 0.9,
      exposure: 0.9,
      glare: 0.02,
      skew_deg: 0.4,
      resolution_ok: true,
      verdict: 'ok',
      reason: null,
    },
  };
}

function result(images: ImageReport[] = [imageReport(0, 'front')]): VerificationResult {
  return {
    request_id: 'req_1',
    aggregate: aggregate({ recommendation: 'return_for_correction' }),
    fields: [
      fieldResult('government_warning', 'mismatch'),
      fieldResult('brand_name', 'match'),
    ],
    images,
    timings_ms: {
      ingest: 1, quality: 1, preprocess: 1, extract: 1, compare: 1,
      adjudicate: null, total: 5,
    },
    cost: {
      input_tokens: 0, output_tokens: 0, cache_read_tokens: 0,
      cache_creation_tokens: 0, usd: 0,
    },
  };
}

function item(overrides: Partial<BatchItem> = {}): BatchItem {
  return {
    item_id: 'item_1',
    job_id: 'job_1',
    row: 4,
    state: 'done',
    attempts: 1,
    application: APPLICATION,
    images: ['front.png'],
    result: result(),
    failure: null,
    decisions: {},
    created_at: 0,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Open the reasoning panel for a row, where the two decision buttons live. */
async function openRow(user: ReturnType<typeof userEvent.setup>, testId: string) {
  const row = screen.getByTestId(testId);
  await user.click(within(row).getByRole('button', { name: /why this verdict/i }));
}

describe('the label picture', () => {
  it('shows the item image, fetched by job and item rather than by filename', () => {
    render(<ItemDetail item={item()} onClose={() => undefined} />);

    const picture = screen.getByRole('img', { name: /label picture/i });
    // The endpoint, not the manifest's filename. A filename is not addressable — two rows
    // in a batch may both name front.png and mean different bottles.
    expect(picture).toHaveAttribute('src', '/batch/job_1/items/item_1/images/0');
  });

  it('outlines the row that needs attention, and numbers it', () => {
    render(<ItemDetail item={item()} onClose={() => undefined} />);

    // The mismatch is outlined and carries number 1. Two controls answer to that name by
    // design — the marker on the picture and its entry in the legend below it — and both
    // highlight the same region, which is the pairing `EvidenceOverlay` is built around.
    expect(screen.getByTestId('region-government_warning')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /government warning/i })).toHaveLength(2);
    // The matching row is not drawn unless the agent asks for it. It has a region — so
    // hovering the row can still light up the words — but an outline on every settled
    // field is seven boxes competing with the one that matters.
    expect(screen.queryByTestId('region-brand_name')).not.toBeInTheDocument();
  });

  it('offers a picture switcher only when there is more than one picture', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<ItemDetail item={item()} onClose={() => undefined} />);
    expect(screen.queryByRole('group', { name: /which picture/i })).not.toBeInTheDocument();
    unmount();

    render(
      <ItemDetail
        item={item({
          images: ['front.png', 'back.png'],
          result: result([imageReport(0, 'front'), imageReport(1, 'back')]),
        })}
        onClose={() => undefined}
      />,
    );

    const group = screen.getByRole('group', { name: /which picture/i });
    await user.click(within(group).getByRole('button', { name: 'back' }));

    expect(screen.getByRole('img', { name: /back/i })).toHaveAttribute(
      'src',
      '/batch/job_1/items/item_1/images/1',
    );
  });

  it('draws on the server copy when it names one, and says so when it does not', () => {
    // The boxes are measured against the preprocessed image. Claiming pixel accuracy over
    // the picture as submitted would be a false trust signal, so the panel admits it.
    render(<ItemDetail item={item()} onClose={() => undefined} />);
    expect(screen.getByText(/can sit a little off/i)).toBeInTheDocument();
  });
});

describe('decisions outlive the dialog', () => {
  it('shows the rulings the server already holds, rather than starting blank', async () => {
    const user = userEvent.setup();
    render(
      <ItemDetail
        item={item({ decisions: { government_warning: 'overridden' } })}
        onClose={() => undefined}
      />,
    );
    await openRow(user, 'row-government_warning');

    expect(screen.getByRole('button', { name: 'I disagree' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('sends the ruling to the server instead of keeping it in the modal', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body ?? '{}'));
      return new Response(
        JSON.stringify({ ...item(), decisions: body.decisions }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      );
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    render(<ItemDetail item={item()} onClose={() => undefined} />);
    await openRow(user, 'row-government_warning');
    await user.click(screen.getByRole('button', { name: 'I agree' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/batch/job_1/items/item_1/decisions');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(String(init.body))).toEqual({
      decisions: { government_warning: 'confirmed' },
    });
  });

  it('clears a ruling with an explicit null, so it can be taken back', async () => {
    const sent: string[] = [];
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      sent.push(String(init?.body ?? ''));
      return new Response(JSON.stringify({ ...item(), decisions: {} }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    render(
      <ItemDetail
        item={item({ decisions: { government_warning: 'confirmed' } })}
        onClose={() => undefined}
      />,
    );
    await openRow(user, 'row-government_warning');
    await user.click(screen.getByRole('button', { name: 'I agree' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    // Omitting the key would mean "change nothing", and a ruling could never be undone.
    expect(JSON.parse(sent[0]!)).toEqual({ decisions: { government_warning: null } });
  });

  it('never leaves a failed write looking saved', async () => {
    // The expensive failure is not the lost click. It is an agent walking away from a
    // queue believing a row was recorded when the server never heard about it.
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(JSON.stringify({ error: { kind: 'internal', code: 'boom', message: 'x' } }), {
            status: 500,
            headers: { 'content-type': 'application/json' },
          }),
      ),
    );

    const user = userEvent.setup();
    render(<ItemDetail item={item()} onClose={() => undefined} />);
    await openRow(user, 'row-government_warning');

    const agree = screen.getByRole('button', { name: 'I agree' });
    await user.click(agree);

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.getByRole('alert')).toHaveTextContent(/unchanged/i);
    // Sprung back, because that is what the server holds.
    expect(agree).toHaveAttribute('aria-pressed', 'false');
    expect(screen.queryByText(/you agreed with this row/i)).not.toBeInTheDocument();
  });

  it('keeps the last ruling when two writes on one row land out of order', async () => {
    // Agree, then disagree, with the first response deliberately slower. Without the
    // per-row sequence guard the row ends up showing whichever the network delivered last.
    const responses: (() => void)[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_input: RequestInfo | URL, init?: RequestInit) =>
          new Promise<Response>((resolve) => {
            const body = JSON.parse(String(init?.body ?? '{}'));
            responses.push(() =>
              resolve(
                new Response(JSON.stringify({ ...item(), decisions: body.decisions }), {
                  status: 200,
                  headers: { 'content-type': 'application/json' },
                }),
              ),
            );
          }),
      ),
    );

    const user = userEvent.setup();
    render(<ItemDetail item={item()} onClose={() => undefined} />);
    await openRow(user, 'row-government_warning');
    await user.click(screen.getByRole('button', { name: 'I agree' }));
    await user.click(screen.getByRole('button', { name: 'I disagree' }));

    await waitFor(() => expect(responses).toHaveLength(2));
    responses[1]!(); // the later write answers first
    responses[0]!(); // the earlier one straggles in behind it

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'I disagree' })).toHaveAttribute(
        'aria-pressed',
        'true',
      ),
    );
    expect(screen.getByRole('button', { name: 'I agree' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });
});

describe('an item that could not be checked', () => {
  it('renders its own state rather than an empty picture panel', () => {
    render(
      <ItemDetail
        item={item({
          state: 'failed',
          result: null,
          images: [],
          attempts: 2,
          failure: {
            code: 'provider_unavailable',
            message: 'The checking service did not answer.',
            next_step: 'retry',
            attempts: 2,
          },
        })}
        onClose={() => undefined}
      />,
    );

    expect(screen.getByText(/was not checked/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing here is a finding/i)).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });
});
