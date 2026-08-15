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

import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
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
