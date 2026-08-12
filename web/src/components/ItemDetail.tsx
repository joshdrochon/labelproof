/**
 * One application from a batch, opened (LP-174).
 *
 * The whole point of this component is that it does **not** invent a second way to say
 * "mismatch". It reuses `AggregateBanner` and `FieldRow` — the same components Verify Now
 * renders — so an agent who has learned to read one screen has learned to read both. A
 * batch-specific verdict layout would be a second vocabulary for the same six verdicts,
 * and the one thing worse than a confusing screen is two screens that disagree.
 *
 * What it adds is only what batch context needs: which row this was, and what to do when
 * the item failed outright and there are no verdicts to show at all.
 *
 * Rendered as a modal dialog. Focus moves in on open and returns to the trigger on close;
 * Escape closes it. Those are not polish — a table you cannot get out of with the keyboard
 * is a table half the users cannot use (UX-4).
 */

import { useEffect, useRef, useState } from 'react';

import AggregateBanner from './AggregateBanner';
import FieldRow from './FieldRow';
import { attentionFields, settledFields } from '../triage';
import type { AgentDecision, BatchItem, FieldName } from '../types';

export default function ItemDetail({
  item,
  onClose,
}: {
  item: BatchItem;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDivElement | null>(null);
  const heading = useRef<HTMLHeadingElement | null>(null);
  const [expanded, setExpanded] = useState<FieldName | null>(null);
  const [decisions, setDecisions] = useState<Partial<Record<FieldName, AgentDecision>>>({});

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    heading.current?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== 'Tab' || !dialog.current) return;
      // Keep Tab inside the dialog. Without this, tabbing walks out into the table
      // behind it while the dialog is still covering the screen.
      const focusable = dialog.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      if (focusable.length === 0) return;
      const first = focusable[0] as HTMLElement;
      const last = focusable[focusable.length - 1] as HTMLElement;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('keydown', onKey, true);
      previous?.focus();
    };
  }, [onClose]);

  const result = item.result;
  const commodity = item.application?.commodity ?? 'spirits';

  return (
    <div className="overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="item-detail-heading"
        ref={dialog}
      >
        <div className="drawer__head">
          <h2 className="drawer__heading" id="item-detail-heading" tabIndex={-1} ref={heading}>
            Row {item.row}
            {item.application?.brand_name ? ` — ${item.application.brand_name}` : ''}
          </h2>
          <button type="button" className="btn" onClick={onClose}>
            Close
          </button>
        </div>

        {result ? (
          <>
            <AggregateBanner
              aggregate={result.aggregate}
              fields={result.fields}
              elapsedMs={result.timings_ms.total}
            />

            {/* A TABLE, not a list.
                
                `FieldRow` renders `<tr>` elements — it is written for the checklist in
                VerifyNow — and the first version of this component wrapped them in
                `<ul>/<li>`. React said so out loud ("`<li>` cannot contain a nested
                `<tr>`") and the accessibility audit is what surfaced it: invalid nesting
                strips the table semantics a screen reader navigates by, so the row/column
                relationship an agent relies on simply is not announced. The same
                components must also produce the same STRUCTURE, or reusing them buys the
                look and not the behaviour. */}
            <table className="checklist">
              <caption className="visually-hidden">
                Field by field comparison of the label against the application.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Field</th>
                  <th scope="col">The application says</th>
                  <th scope="col">The label shows</th>
                  <th scope="col">Verdict</th>
                </tr>
              </thead>

              <tbody className="checklist__group" data-group="attention">
                {attentionFields(result.fields).map((field, index) => (
                  <FieldRow
                    key={field.field}
                    result={field}
                    commodity={commodity}
                    variant="attention"
                    number={index + 1}
                    expanded={expanded === field.field}
                    onToggle={() =>
                      setExpanded((current) => (current === field.field ? null : field.field))
                    }
                    onActivate={() => undefined}
                    decision={decisions[field.field] ?? null}
                    onDecide={(decision) =>
                      setDecisions((current) => ({ ...current, [field.field]: decision ?? undefined }))
                    }
                    isFocused={false}
                  />
                ))}
              </tbody>

              <tbody className="checklist__group" data-group="settled">
                {settledFields(result.fields).map((field) => (
                  <FieldRow
                    key={field.field}
                    result={field}
                    commodity={commodity}
                    variant="settled"
                    number={null}
                    expanded={expanded === field.field}
                    onToggle={() =>
                      setExpanded((current) => (current === field.field ? null : field.field))
                    }
                    onActivate={() => undefined}
                    decision={decisions[field.field] ?? null}
                    onDecide={(decision) =>
                      setDecisions((current) => ({ ...current, [field.field]: decision ?? undefined }))
                    }
                    isFocused={false}
                  />
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <div className="drawer__failure">
            <h3>This application was not checked</h3>
            <p>
              {item.failure?.message ??
                'The check did not finish, so nothing about this label has been verified.'}
            </p>
            <p className="muted">
              Nothing here is a finding against the label. Retry the batch, or check this
              one on the Verify now screen.
            </p>
            {item.attempts > 1 ? (
              <p className="muted">Tried {item.attempts} times.</p>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}
