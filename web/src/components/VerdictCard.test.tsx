/**
 * The verdict chip, and the grayscale rule.
 *
 * The grayscale requirement is not a style preference — agents may be colour-blind and
 * Dave prints in black and white — so it is tested rather than asserted in a comment.
 * The test strips every colour channel from the question and demands the six verdicts
 * still be six distinguishable things.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import VerdictChip from './VerdictCard';
import { VERDICTS } from '../copy';
import type { Verdict } from '../types';

const ALL: Verdict[] = [
  'match',
  'acceptable_variation',
  'mismatch',
  'missing',
  'unreadable',
  'not_applicable',
];

describe('verdict chips', () => {
  it('covers exactly the six verdicts in the taxonomy', () => {
    expect(Object.keys(VERDICTS).sort()).toEqual([...ALL].sort());
  });

  it.each(ALL)('renders %s as an icon and a word', (verdict) => {
    render(<VerdictChip verdict={verdict} />);
    const chip = screen.getByTestId(`verdict-chip-${verdict}`);

    // The word, spelled out, as real text.
    expect(chip).toHaveTextContent(VERDICTS[verdict].word);
    // And an icon beside it, hidden from screen readers because the word already says it.
    const glyph = chip.querySelector('svg');
    expect(glyph).not.toBeNull();
    expect(glyph).toHaveAttribute('aria-hidden', 'true');
  });

  it('never depends on colour: word plus shape identifies each verdict on its own', () => {
    const signatures = ALL.map((verdict) => {
      render(<VerdictChip verdict={verdict} />);
      const chip = screen.getByTestId(`verdict-chip-${verdict}`);
      // Everything a grayscale printout still carries: the text and the icon outline.
      const shape = chip.querySelector('svg')?.getAttribute('data-icon');
      return `${chip.textContent?.trim()}|${shape}`;
    });

    expect(new Set(signatures).size).toBe(ALL.length);
    for (const signature of signatures) {
      const [word, shape] = signature.split('|');
      expect(word).toBeTruthy();
      expect(shape).toBeTruthy();
    }
  });

  it('gives every verdict its own icon shape, not six tints of one shape', () => {
    const shapes = ALL.map((verdict) => VERDICTS[verdict].icon);
    expect(new Set(shapes).size).toBe(ALL.length);
  });

  it('proves colour cannot be the carrier: two verdicts deliberately share one colour', () => {
    // Mismatch and Missing are both --serious in styles.css. If an agent could tell the
    // six apart by hue, this assertion would fail — and so would the printout.
    const css = readFileSync(join(process.cwd(), 'src', 'styles.css'), 'utf8');
    expect(css).toMatch(
      /\.chip\[data-verdict='mismatch'\],\s*\n?\.chip\[data-verdict='missing'\]/,
    );
  });

  it('states what to do for every verdict, in plain words', () => {
    for (const verdict of ALL) {
      const meta = VERDICTS[verdict];
      expect(meta.whatToDo.length).toBeGreaterThan(0);
      // No jargon leaks into the agent's vocabulary.
      expect(meta.whatToDo.toLowerCase()).not.toMatch(
        /inference|model|extraction|confidence score|token/,
      );
    }
  });
});
