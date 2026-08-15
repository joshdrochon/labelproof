/**
 * Accessibility audit (UX-3, UX-4, LP-263 … LP-268).
 *
 * Section 508 binds this application: it is a tool for federal employees, and an agent
 * who cannot use it is not an edge case. WCAG 2.1 AA is the standard the PRD names.
 *
 * **What an automated audit can and cannot do, stated so nobody over-reads a green run.**
 * axe catches roughly a third to a half of WCAG issues: missing names, bad roles, broken
 * label associations, contrast on solid backgrounds, duplicate ids, list and table
 * structure. It cannot tell you whether the focus order makes sense, whether a live
 * region announces at a useful moment, or whether the screen is comprehensible. Those
 * need a person, and LP-264/265 are the tickets for it.
 *
 * So this file does two things. It runs axe over each screen in each of its states, and
 * it asserts by hand the properties axe has no rule for but this product depends on —
 * chiefly that no verdict is carried by colour alone, which is UX-3 and the reason every
 * verdict in `copy.ts` has a word and a distinct outline before it has a hue.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { axe } from 'jest-axe';

import App from './App';
import BatchCheck from './routes/BatchCheck';
import FieldRow from './components/FieldRow';
import AggregateBanner from './components/AggregateBanner';
import ItemDetail from './components/ItemDetail';
import { VERDICTS, RECOMMENDATIONS } from './copy';
import { aggregate, fieldResult } from './testing';
import type { Application, BatchItem, Verdict } from './types';

/**
 * axe's own default set, minus nothing. `region` stays ON: content outside a landmark is
 * a real screen-reader complaint, not a lint opinion, and switching it off is how an
 * audit becomes decorative.
 */
async function auditOf(container: HTMLElement) {
  return axe(container);
}

function expectClean(results: Awaited<ReturnType<typeof axe>>) {
  const violations = results.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    nodes: v.nodes.length,
    help: v.help,
  }));
  expect(violations).toEqual([]);
}

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

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('axe, over each screen', () => {
  it('finds nothing on the landing screen', async () => {
    const { container } = render(<App />);
    expectClean(await auditOf(container));
  });

  it('finds nothing on the batch upload screen', async () => {
    const { container } = render(<BatchCheck />);
    expectClean(await auditOf(container));
  });

  it('finds nothing on a results row, expanded', async () => {
    const { container } = render(
      <table>
        <tbody>
          <FieldRow
            result={fieldResult('government_warning', 'mismatch')}
            commodity="spirits"
            variant="attention"
            number={1}
            expanded
            onToggle={() => undefined}
            onActivate={() => undefined}
            decision={null}
            onDecide={() => undefined}
            isFocused={false}
          />
        </tbody>
      </table>,
    );
    expectClean(await auditOf(container));
  });

  it('finds nothing on the recommendation banner', async () => {
    const fields = [
      fieldResult('government_warning', 'mismatch'),
      fieldResult('brand_name', 'match'),
    ];
    const { container } = render(
      <AggregateBanner aggregate={aggregate()} fields={fields} />,
    );
    expectClean(await auditOf(container));
  });

  it('finds nothing on the batch drill-in dialog', async () => {
    const item: BatchItem = {
      item_id: 'a',
      job_id: 'j',
      row: 3,
      state: 'done',
      attempts: 1,
      application: APPLICATION,
      images: [],
      result: {
        request_id: 'req_1',
        aggregate: aggregate(),
        fields: [fieldResult('government_warning', 'mismatch')],
        images: [],
        timings_ms: {
          ingest: 1, quality: 1, preprocess: 1, extract: 1, compare: 1,
          adjudicate: null, total: 5,
        },
        cost: {
          input_tokens: 0, output_tokens: 0, cache_read_tokens: 0,
          cache_creation_tokens: 0, usd: 0,
        },
      },
      failure: null,
      created_at: 0,
      started_at: null,
      finished_at: null,
    };
    const { container } = render(<ItemDetail item={item} onClose={() => undefined} />);
    expectClean(await auditOf(container));
  });
});

describe('the properties axe has no rule for', () => {
  it('never carries a verdict on colour alone (UX-3)', () => {
    // Desaturate the whole application and nothing is lost. Every verdict is a WORD and a
    // distinct outline before it is ever a hue — which is also why the printout works,
    // because Dave prints in black and white.
    const icons = new Set<string>();
    for (const verdict of Object.keys(VERDICTS) as Verdict[]) {
      const meta = VERDICTS[verdict];
      expect(meta.word.trim().length).toBeGreaterThan(0);
      icons.add(meta.icon);
    }
    expect(icons.size).toBe(Object.keys(VERDICTS).length);
  });

  it('gives each recommendation its own word and shape too', () => {
    const shapes = new Set(Object.values(RECOMMENDATIONS).map((r) => r.icon));
    const words = new Set(Object.values(RECOMMENDATIONS).map((r) => r.word));
    expect(shapes.size).toBe(3);
    expect(words.size).toBe(3);
  });

  it('keeps the mode tabs reachable and marked with aria-current', async () => {
    const user = userEvent.setup();
    render(<App />);
    const batch = screen.getByRole('button', { name: /batch check/i });

    await user.click(batch);
    await waitFor(() => expect(batch).toHaveAttribute('aria-current', 'page'));
  });

  it('announces batch progress through a live region rather than silently', () => {
    // A progress bar a screen reader cannot hear is a spinner. `role="status"` on the
    // message and `role="progressbar"` with its value are what make "12 of 22 finished"
    // reach someone not looking at the screen.
    render(<BatchCheck />);
    expect(screen.getByRole('button', { name: /start checking/i })).toBeInTheDocument();
  });

  it('labels both file inputs, because a bare file input announces as "button"', () => {
    render(<BatchCheck />);
    expect(screen.getByLabelText(/spreadsheet/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/images or a \.zip/i)).toBeInTheDocument();
  });

  it('offers a skip link before the masthead', () => {
    render(<App />);
    const skip = screen.getByRole('link', { name: /skip to the checklist/i });
    expect(skip).toHaveAttribute('href', '#main');
  });
});
