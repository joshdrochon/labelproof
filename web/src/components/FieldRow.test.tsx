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


describe('a row judged by Tier 3 says so', () => {
  it('marks the row when a model made the call', () => {
    // MATCH-5, HITL-4. A verdict that came from a judgement must not look identical to
    // one computed by a rule. The product is advisory, and an agent weighing a row needs
    // to know which kind of answer they are weighing.
    render(
      <table>
        <tbody>
          <FieldRow
            result={fieldResult('producer', 'acceptable_variation', {
              tier: 3,
              rationale: 'Checked by a second model because the wording differs: same name, reversed.',
            })}
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

    expect(screen.getByText(/a second model judged this row/i)).toBeInTheDocument();
    expect(screen.getByText(/that is a judgement, not a rule/i)).toBeInTheDocument();
  });

  it('does not mark rows that a rule settled', () => {
    // Tiers 1 and 2 are deterministic comparisons. Badging everything would make the
    // marker meaningless, which is the usual way a warning label stops being read.
    render(
      <table>
        <tbody>
          <FieldRow
            result={fieldResult('producer', 'acceptable_variation', { tier: 2 })}
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

    expect(screen.queryByText(/a second model judged/i)).not.toBeInTheDocument();
  });
});


describe('the empty "label shows" cell', () => {
  const render_ = (verdict: Parameters<typeof fieldResult>[1]) =>
    render(
      <table>
        <tbody>
          <FieldRow
            result={fieldResult('government_warning', verdict, { extracted: null })}
            commodity="spirits"
            variant="attention"
            number={1}
            expanded={false}
            onToggle={() => undefined}
            onActivate={() => undefined}
            decision={null}
            onDecide={() => undefined}
            isFocused={false}
          />
        </tbody>
      </table>,
    );

  it('does not claim the element is absent when nobody could read it', () => {
    // `extracted` is always null on an Unreadable row, so every one of them used to say
    // "Not found on the label" — a finding against the artwork printed in place of a
    // statement about the photograph. A pre-gated upload put that sentence on all seven
    // rows of a label no model had seen.
    render_('unreadable');
    expect(screen.getByText('Could not be read')).toBeInTheDocument();
    expect(screen.queryByText('Not found on the label')).not.toBeInTheDocument();
  });

  it('still says so when the element genuinely is not there', () => {
    // Missing IS the finding. The fix must not soften it.
    render_('missing');
    expect(screen.getByText('Not found on the label')).toBeInTheDocument();
  });

  it('does not read as a problem when the field does not apply', () => {
    render_('not_applicable');
    expect(screen.getByText('Not required here')).toBeInTheDocument();
  });
});
