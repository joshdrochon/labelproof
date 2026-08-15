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

import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

/**
 * Make sure a row's reasoning panel — where the decision buttons live — is open.
 *
 * Attention rows open themselves, so this clicks only when one is shut. Clicking
 * unconditionally is what these tests used to do, and once the rows started opening by
 * default that click was closing them again.
 */
async function revealRow(user: ReturnType<typeof userEvent.setup>, testId: string) {
  const toggle = within(screen.getByTestId(testId)).getByRole('button', {
    name: /why this verdict|hide the reason/i,
  });
  if (toggle.getAttribute('aria-expanded') !== 'true') await user.click(toggle);
}

/**
 * The dialog as the batch screen actually mounts it: the parent owns `decisions` and
 * feeds each PATCH answer back down. Rendering it uncontrolled would let a decision look
 * like it stuck when nothing was holding it — which is the bug these tests exist for.
 */
function Harness({ item: initial }: { item: BatchItem }) {
  const [decisions, setDecisions] = useState(initial.decisions);
  return (
    <ItemDetail
      item={{ ...initial, decisions }}
      onClose={() => undefined}
      onDecisions={setDecisions}
    />
  );
}

describe('the label picture', () => {
  it('shows the item image, fetched by job and item rather than by filename', () => {
    render(<ItemDetail item={item()} onClose={() => undefined} onDecisions={() => undefined} />);

    const picture = screen.getByRole('img', { name: /label picture/i });
    // The endpoint, not the manifest's filename. A filename is not addressable — two rows
    // in a batch may both name front.png and mean different bottles.
    expect(picture).toHaveAttribute('src', '/batch/job_1/items/item_1/images/0');
  });

  it('outlines the row that needs attention, and numbers it', () => {
    render(<ItemDetail item={item()} onClose={() => undefined} onDecisions={() => undefined} />);

    // The mismatch is outlined and carries number 1. Two controls answer to that name
    // inside the picture panel by design — the marker on the photo and its entry in the
    // legend below it — and both highlight the same region, which is the pairing
    // `EvidenceOverlay` is built around. (The banner carries a third, its jump link,
    // which is why this is scoped to the figure rather than the whole dialog.)
    expect(screen.getByTestId('region-government_warning')).toBeInTheDocument();
    expect(
      within(screen.getByRole('figure')).getAllByRole('button', {
        name: /government warning/i,
      }),
    ).toHaveLength(2);
    // The matching row is not drawn unless the agent asks for it. It has a region — so
    // hovering the row can still light up the words — but an outline on every settled
    // field is seven boxes competing with the one that matters.
    expect(screen.queryByTestId('region-brand_name')).not.toBeInTheDocument();
  });

  it('offers a picture switcher only when there is more than one picture', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<ItemDetail item={item()} onClose={() => undefined} onDecisions={() => undefined} />);
    expect(screen.queryByRole('group', { name: /which picture/i })).not.toBeInTheDocument();
    unmount();

    render(
      <ItemDetail
        item={item({
          images: ['front.png', 'back.png'],
          result: result([imageReport(0, 'front'), imageReport(1, 'back')]),
        })}
        onClose={() => undefined}
        onDecisions={() => undefined}
      />,
    );

    const group = screen.getByRole('group', { name: /which picture/i });
    await user.click(within(group).getByRole('button', { name: 'back' }));

    expect(screen.getByRole('img', { name: /back/i })).toHaveAttribute(
      'src',
      '/batch/job_1/items/item_1/images/1',
    );
  });

  it('says the geometry is approximate when it falls back to the submitted picture', () => {
    // The boxes are measured against the preprocessed image. Claiming pixel accuracy over
    // the picture as submitted would be a false trust signal, so the panel admits it.
    render(<ItemDetail item={item()} onClose={() => undefined} onDecisions={() => undefined} />);
    expect(screen.getByText(/can sit a little off/i)).toBeInTheDocument();
  });

  it('draws on the server copy when it names one, and drops the disclosure', () => {
    // The other half of the branch above, which went untested: when the server names the
    // preprocessed image the outlines DO land exactly, and repeating the apology anyway
    // would train agents to ignore it on the labels where it is true.
    const withUrl = imageReport(0, 'front');
    withUrl.url = '/preprocessed/front.webp';
    render(
      <ItemDetail
        item={item({ result: result([withUrl]) })}
        onClose={() => undefined}
        onDecisions={() => undefined}
      />,
    );

    expect(screen.getByRole('img', { name: /label picture/i })).toHaveAttribute(
      'src',
      '/preprocessed/front.webp',
    );
    expect(screen.queryByText(/can sit a little off/i)).not.toBeInTheDocument();
  });

  it('judges each picture on its own, not the item as a whole', async () => {
    // A server that named the front and not the back used to mark BOTH exact, then draw
    // the back's outlines over the un-deskewed original without a word.
    const front = imageReport(0, 'front');
    front.url = '/preprocessed/front.webp';
    // One outlined row per picture, so the disclosure is reachable on both — it only
    // shows where there is a region for it to be about.
    const fields = [
      fieldResult('brand_name', 'mismatch'),
      fieldResult('government_warning', 'mismatch', {
        evidence: { image_index: 1, bbox: { x0: 0.1, y0: 0.7, x1: 0.9, y1: 0.9 } },
      }),
    ];
    const user = userEvent.setup();
    render(
      <ItemDetail
        item={item({
          images: ['front.png', 'back.png'],
          result: { ...result([front, imageReport(1, 'back')]), fields },
        })}
        onClose={() => undefined}
        onDecisions={() => undefined}
      />,
    );

    expect(screen.queryByText(/can sit a little off/i)).not.toBeInTheDocument();
    await user.click(
      within(screen.getByRole('group', { name: /which picture/i })).getByRole('button', {
        name: 'back',
      }),
    );
    expect(screen.getByText(/can sit a little off/i)).toBeInTheDocument();
  });

  it('numbers a row from its region, not from its place in the checklist', () => {
    // A `missing` warning sorts FIRST and carries no bbox, so it produces no region. The
    // drill-in used to number rows by position, which made the row below it say "outlined
    // as 2" while its outline was the only one drawn and numbered 1.
    const fields = [
      fieldResult('government_warning', 'missing', { evidence: null }),
      fieldResult('brand_name', 'mismatch'),
    ];
    render(
      <ItemDetail
        item={item({ result: { ...result(), fields } })}
        onClose={() => undefined}
        onDecisions={() => undefined}
      />,
    );

    // One region exists and it is numbered 1. The brand-name row must claim that number.
    expect(screen.getByTestId('region-brand_name')).toBeInTheDocument();
    const brandRow = within(screen.getByTestId('row-brand_name'));
    expect(brandRow.getByText(/outlined as 1 on the picture/i)).toBeInTheDocument();
    // And the row with no evidence claims no outline at all.
    expect(
      within(screen.getByTestId('row-government_warning')).queryByText(/outlined as/i),
    ).not.toBeInTheDocument();
  });

  /**
   * A two-sided item with one outlined row per face — brand name on the front, government
   * warning on the back. Both are needed: a hover test can only prove anything if it ends
   * on a row pointing at the picture that is NOT showing.
   */
  function twoSided() {
    return item({
      images: ['front.png', 'back.png'],
      result: {
        ...result([imageReport(0, 'front'), imageReport(1, 'back')]),
        fields: [
          fieldResult('brand_name', 'mismatch'),
          fieldResult('government_warning', 'mismatch', {
            evidence: { image_index: 1, bbox: { x0: 0.1, y0: 0.7, x1: 0.9, y1: 0.9 } },
          }),
        ],
      },
    });
  }

  it('turns to the picture a row is outlined on when the agent asks for that row', async () => {
    // "Outlined as 1 on the picture" is a lie on a two-sided label if the outline is on
    // the face the agent is not looking at and nothing takes them there. The banner's row
    // link is the deliberate ask.
    const user = userEvent.setup();
    render(<ItemDetail item={twoSided()} onClose={() => undefined} onDecisions={() => undefined} />);

    // Starts on the front, where the region is not drawn.
    expect(screen.queryByTestId('region-government_warning')).not.toBeInTheDocument();

    await user.click(
      within(screen.getByRole('region', { name: /recommendation/i })).getByRole('button', {
        name: /government warning/i,
      }),
    );

    expect(screen.getByRole('img', { name: /back/i })).toBeInTheDocument();
    expect(screen.getByTestId('region-government_warning')).toBeInTheDocument();
  });

  it('never moves the picture on hover, and never overrides the agent\'s choice', async () => {
    // Hover is a highlight, not a page turn (LP-104). Wiring the switch to `onActivate`
    // meant sweeping the mouse down the checklist flipped to the back and left it there —
    // `mouseleave` names no region, so nothing turned back — and it beat the agent's own
    // switcher choice, making a two-sided label impossible to hold still while reading.
    const user = userEvent.setup();
    render(<ItemDetail item={twoSided()} onClose={() => undefined} onDecisions={() => undefined} />);

    // Opening on the front, brushing the row outlined on the back must not turn the page.
    await user.hover(screen.getByTestId('row-government_warning'));
    expect(screen.getByRole('img', { name: /front/i })).toBeInTheDocument();
    await user.unhover(screen.getByTestId('row-government_warning'));
    expect(screen.getByRole('img', { name: /front/i })).toBeInTheDocument();

    // And once the agent has chosen the back, reading a row outlined on the FRONT must
    // not pull it away. Hovering a back row here would prove nothing.
    await user.click(
      within(screen.getByRole('group', { name: /which picture/i })).getByRole('button', {
        name: 'back',
      }),
    );
    await user.hover(screen.getByTestId('row-brand_name'));
    expect(screen.getByRole('img', { name: /back/i })).toBeInTheDocument();
  });

  it('ignores a region naming a picture the item does not have', async () => {
    // `image_index` comes from the server. Unclamped it selected an index with no URL
    // behind it — the panel fell back to "not available", and the switcher only renders
    // for two or more pictures, so there was no way back to the label at all.
    const user = userEvent.setup();
    render(
      <ItemDetail
        item={item({
          images: ['front.png'],
          result: {
            ...result([imageReport(0, 'front')]),
            fields: [
              fieldResult('government_warning', 'mismatch', {
                evidence: { image_index: 4, bbox: { x0: 0.1, y0: 0.7, x1: 0.9, y1: 0.9 } },
              }),
            ],
          },
        })}
        onClose={() => undefined}
        onDecisions={() => undefined}
      />,
    );

    await user.click(
      within(screen.getByRole('region', { name: /recommendation/i })).getByRole('button', {
        name: /government warning/i,
      }),
    );

    // The one picture it has is still on screen.
    expect(screen.getByRole('img', { name: /label picture/i })).toBeInTheDocument();
    expect(screen.queryByText(/not available to display/i)).not.toBeInTheDocument();
  });

  it('says the picture is missing rather than drawing a broken image', () => {
    render(<ItemDetail item={item()} onClose={() => undefined} onDecisions={() => undefined} />);

    fireEvent.error(screen.getByRole('img', { name: /label picture/i }));

    // The checklist is unaffected and says so — the verdicts are still readable, it is
    // only their citation that is missing.
    expect(screen.getByText(/not available to display/i)).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(screen.getByTestId('row-government_warning')).toBeInTheDocument();
  });
});

describe('decisions outlive the dialog', () => {
  it('shows the rulings the server already holds, rather than starting blank', async () => {
    const user = userEvent.setup();
    render(<Harness item={item({ decisions: { government_warning: 'overridden' } })} />);
    await revealRow(user, 'row-government_warning');

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
    render(<Harness item={item()} />);
    await revealRow(user, 'row-government_warning');
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
    render(<Harness item={item({ decisions: { government_warning: 'confirmed' } })} />);
    await revealRow(user, 'row-government_warning');
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
    render(<Harness item={item()} />);
    await revealRow(user, 'row-government_warning');

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
    render(<Harness item={item()} />);
    await revealRow(user, 'row-government_warning');
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
        onDecisions={() => undefined}
      />,
    );

    expect(screen.getByText(/was not checked/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing here is a finding/i)).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });
});
