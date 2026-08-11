/**
 * The recommendation banner.
 *
 * What is being defended here is the posture, not the pixels: the app advises and the
 * agent decides, so "Recommendation:" is present on all three outcomes and no sentence
 * in the banner is an instruction. The banner also has to name the rows that need eyes —
 * a bare "Needs review" sends an agent hunting.
 */

import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import AggregateBanner, { formatElapsed } from './AggregateBanner';
import { aggregate, fieldResult } from '../testing';
import type { Recommendation } from '../types';

const FIELDS = [
  fieldResult('government_warning', 'mismatch'),
  fieldResult('alcohol_content', 'mismatch'),
  fieldResult('brand_name', 'match'),
  fieldResult('class_type', 'match'),
  fieldResult('net_contents', 'match'),
  fieldResult('producer', 'match'),
  fieldResult('country_of_origin', 'not_applicable'),
];

const WORDS: Record<Recommendation, string> = {
  ready_to_approve: 'Ready to approve',
  needs_review: 'Needs review',
  return_for_correction: 'Return for correction',
};

describe('aggregate banner', () => {
  it.each(Object.keys(WORDS) as Recommendation[])(
    'prefixes %s with "Recommendation:" and never phrases it as an order',
    (recommendation) => {
      render(
        <AggregateBanner
          aggregate={aggregate({ recommendation })}
          fields={FIELDS}
          elapsedMs={3800}
        />,
      );
      const heading = screen.getByRole('heading', { level: 2 });
      expect(heading).toHaveTextContent('Recommendation:');
      expect(heading).toHaveTextContent(WORDS[recommendation]);

      // The banner advises. It does not contain a control that decides anything.
      const banner = screen.getByRole('region', { name: /recommendation/i });
      const decisions = within(banner).queryAllByRole('button', {
        name: /^(approve|reject|deny)$/i,
      });
      expect(decisions).toHaveLength(0);
    },
  );

  it('names which rows need eyes, worst first with the warning ahead of the rest', () => {
    render(
      <AggregateBanner
        aggregate={aggregate()}
        fields={FIELDS}
        elapsedMs={3800}
        onJumpToField={() => undefined}
      />,
    );
    expect(screen.getByText(/rows to look at/i)).toBeInTheDocument();

    const named = screen
      .getAllByRole('button')
      .map((node) => node.textContent?.trim())
      .filter(Boolean);
    expect(named).toEqual(['Government warning', 'Alcohol content']);
  });

  it('says so plainly when nothing needs a second look', () => {
    const clean = FIELDS.map((row) => ({ ...row, verdict: 'match' as const }));
    render(
      <AggregateBanner
        aggregate={aggregate({ recommendation: 'ready_to_approve' })}
        fields={clean}
        elapsedMs={900}
      />,
    );
    expect(screen.getByText(/no row needs a second look/i)).toBeInTheDocument();
  });

  it('shows how long the check took', () => {
    render(<AggregateBanner aggregate={aggregate()} fields={FIELDS} elapsedMs={3800} />);
    expect(screen.getByTestId('elapsed')).toHaveTextContent('Checked in 3.8 seconds');
  });

  it('phrases a sub-second check without a misleading 0.0', () => {
    expect(formatElapsed(640)).toBe('in under a second');
    expect(formatElapsed(0)).toBe('');
  });

  it('lets an agent jump straight to a named row', async () => {
    const onJump = vi.fn();
    render(
      <AggregateBanner
        aggregate={aggregate()}
        fields={FIELDS}
        elapsedMs={3800}
        onJumpToField={onJump}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Government warning' }));
    expect(onJump).toHaveBeenCalledWith('government_warning');
  });
});
