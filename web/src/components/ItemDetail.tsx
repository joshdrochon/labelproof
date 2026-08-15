/**
 * One application from a batch, opened (LP-174, HITL-3).
 *
 * The whole point of this component is that it does **not** invent a second way to say
 * "mismatch". It reuses `AggregateBanner`, `FieldRow` and `EvidenceOverlay` — the same
 * components Verify Now renders, fed by the same `evidenceRegions()` — so an agent who
 * has learned to read one screen has learned to read both. A batch-specific verdict
 * layout would be a second vocabulary for the same six verdicts, and the one thing worse
 * than a confusing screen is two screens that disagree.
 *
 * **The picture is not decoration.** This dialog shipped without one, which meant an
 * agent triaging a brand-name mismatch across three hundred applications could read the
 * claim that the label disagreed with the application and had no way to look at the
 * label. That is the tool asking to be believed, which is the posture the whole product
 * is built to avoid. The outlines are the evidence; without them the drill-in is a
 * verdict with a citation nobody can follow.
 *
 * What it adds beyond Verify Now is only what batch context needs: which row this was,
 * and what to do when the item failed outright and there are no verdicts to show at all.
 * A failed item has no result, no images worth drawing and no regions — it must still say
 * so plainly rather than rendering an empty panel beside an empty checklist.
 *
 * Rendered as a modal dialog. Focus moves in on open and returns to the trigger on close;
 * Escape closes it. Those are not polish — a table you cannot get out of with the keyboard
 * is a table half the users cannot use (UX-4).
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import AggregateBanner from './AggregateBanner';
import EvidenceOverlay from './EvidenceOverlay';
import FieldRow from './FieldRow';
import { batchItemImageUrl } from '../api';
import { evidenceRegions } from '../evidence';
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
  const [activeField, setActiveField] = useState<FieldName | null>(null);
  const [imageIndex, setImageIndex] = useState(0);

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

  const regions = useMemo(
    () => (result ? evidenceRegions(result.fields) : []),
    [result],
  );

  /**
   * How many pictures this item has.
   *
   * `result.images` is what the pipeline actually processed and carries the indices the
   * evidence boxes are measured against, so it wins. `item.images` — the filenames the
   * manifest row named — is the fallback for an item whose result carries no image report
   * at all, which is better than showing no picture because a field was omitted.
   */
  const pictureCount = result?.images.length || item.images.length;

  /**
   * Prefer a URL the server named; fall back to the item's own image endpoint.
   *
   * Same rule Verify Now applies, and for the same reason. Boxes are measured against the
   * *preprocessed* image. When the server names that copy in `result.images[].url` the
   * outlines land exactly; when it does not, this asks the batch endpoint for the picture
   * as it was submitted, which is the same label before deskew, and the panel says the
   * geometry is approximate rather than implying an accuracy it has not got.
   */
  const reportFor = (index: number) => result?.images.find((image) => image.index === index);
  const urlFor = (index: number): string =>
    reportFor(index)?.url ?? batchItemImageUrl(item.job_id, item.item_id, index);
  const geometryIsApproximate = !result?.images.some((image) => Boolean(image.url));

  const labelFor = (index: number): string => {
    const role = reportFor(index)?.role;
    return role ? `Label picture — ${role}` : `Label picture ${index + 1}`;
  };

  const activeImage = reportFor(imageIndex);
  const qualityNote =
    activeImage && activeImage.quality.verdict !== 'ok'
      ? (activeImage.quality.reason ??
        'This picture was hard to read. Anything marked Unreadable needs a better image or your own eyes on the bottle.')
      : null;

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
            />

            {/* The picture beside the checklist, exactly as Verify Now arranges them.
                When the item has no picture at all the column is dropped rather than
                filled with an apology — the grid collapses and the checklist takes the
                whole width, which is what the dialog did before the panel existed. */}
            <div className="drawer__columns" data-evidence={pictureCount > 0 ? 'true' : 'false'}>
              {pictureCount > 0 ? (
                <div className="drawer__evidence">
                  <EvidenceOverlay
                    imageUrl={urlFor(imageIndex)}
                    imageIndex={imageIndex}
                    imageLabel={labelFor(imageIndex)}
                    regions={regions}
                    activeField={activeField}
                    onActivateField={setActiveField}
                    geometryIsApproximate={geometryIsApproximate}
                    qualityNote={qualityNote}
                  >
                    {pictureCount > 1 ? (
                      <div
                        className="evidence__switch"
                        role="group"
                        aria-label="Which picture to show"
                      >
                        {Array.from({ length: pictureCount }, (_, index) => (
                          <button
                            type="button"
                            key={index}
                            className="btn btn--quiet"
                            aria-pressed={index === imageIndex}
                            onClick={() => setImageIndex(index)}
                          >
                            {reportFor(index)?.role ?? `Picture ${index + 1}`}
                          </button>
                        ))}
                      </div>
                    ) : null}
                  </EvidenceOverlay>
                </div>
              ) : null}

              <div className="drawer__checklist">
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
                        onActivate={(on) => setActiveField(on ? field.field : null)}
                        decision={decisions[field.field] ?? null}
                        onDecide={(decision) =>
                          setDecisions((current) => ({ ...current, [field.field]: decision ?? undefined }))
                        }
                        isFocused={activeField === field.field}
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
                        onActivate={(on) => setActiveField(on ? field.field : null)}
                        decision={decisions[field.field] ?? null}
                        onDecide={(decision) =>
                          setDecisions((current) => ({ ...current, [field.field]: decision ?? undefined }))
                        }
                        isFocused={activeField === field.field}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
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
