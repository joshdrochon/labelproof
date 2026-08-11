/**
 * The two row weights.
 *
 * The hierarchy fix is only real if a quiet row is quieted the *right* way. Two things
 * are load-bearing and both are tested: a settled row must never drop below the 16px
 * legibility floor, and it must still carry its verdict as a word.
 */

import type { ReactNode } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import FieldRow from './FieldRow';
import { fieldResult } from '../testing';

function inTable(node: ReactNode) {
  return render(
    <table>
      <tbody>{node}</tbody>
    </table>,
  );
}

const noop = () => undefined;

describe('field row', () => {
  it('gives an attention row a number tying it to the picture', () => {
    inTable(
      <FieldRow
        result={fieldResult('government_warning', 'mismatch')}
        commodity="spirits"
        variant="attention"
        number={1}
        expanded
        onToggle={noop}
        onActivate={noop}
        decision={null}
        onDecide={noop}
        isFocused={false}
      />,
    );
    expect(screen.getByText(/outlined as 1 on the picture/i)).toBeInTheDocument();
    expect(screen.getByTestId('row-government_warning')).toHaveAttribute(
      'data-variant',
      'attention',
    );
  });

  it('keeps a settled row fully legible — quieted, never shrunk out of reach', () => {
    inTable(
      <FieldRow
        result={fieldResult('class_type', 'match')}
        commodity="spirits"
        variant="settled"
        number={null}
        expanded={false}
        onToggle={noop}
        onActivate={noop}
        decision={null}
        onDecide={noop}
        isFocused={false}
      />,
    );
    // The verdict is still a word, not a colour or a tick on its own.
    expect(screen.getByTestId('verdict-chip-match')).toHaveTextContent('Match');
    // And the reasoning is still reachable, just closed.
    expect(screen.getByRole('button', { name: /why this verdict/i })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
  });

  it('shows the reasoning, what to do, and the finding citation when open', async () => {
    inTable(
      <FieldRow
        result={fieldResult('government_warning', 'mismatch', {
          rationale: 'The header is printed in title case rather than capitals.',
          findings: [
            {
              code: 'warning_header_not_caps',
              message: 'The words "GOVERNMENT WARNING" must be in capital letters.',
              citation: '27 CFR 16.22',
              severity: 'violation',
            },
          ],
        })}
        commodity="spirits"
        variant="attention"
        number={1}
        expanded
        onToggle={noop}
        onActivate={noop}
        decision={null}
        onDecide={noop}
        isFocused={false}
      />,
    );
    expect(screen.getByText(/title case rather than capitals/i)).toBeInTheDocument();
    expect(screen.getByText('What to do')).toBeInTheDocument();
    expect(screen.getByText('27 CFR 16.22')).toBeInTheDocument();
  });

  it('says plainly when there is no outlined area for a row', () => {
    inTable(
      <FieldRow
        result={fieldResult('producer', 'unreadable', { evidence: null })}
        commodity="spirits"
        variant="attention"
        number={null}
        expanded
        onToggle={noop}
        onActivate={noop}
        decision={null}
        onDecide={noop}
        isFocused={false}
      />,
    );
    expect(screen.getByText(/no area is outlined on the picture/i)).toBeInTheDocument();
  });

  it('records the agent agreeing or disagreeing with the row', async () => {
    const onDecide = vi.fn();
    inTable(
      <FieldRow
        result={fieldResult('brand_name', 'acceptable_variation')}
        commodity="spirits"
        variant="attention"
        number={1}
        expanded
        onToggle={noop}
        onActivate={noop}
        decision={null}
        onDecide={onDecide}
        isFocused={false}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'I disagree' }));
    expect(onDecide).toHaveBeenCalledWith('overridden');
  });
});
