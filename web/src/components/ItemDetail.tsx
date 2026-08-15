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

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import AggregateBanner from './AggregateBanner';
import EvidenceOverlay from './EvidenceOverlay';
import FieldRow from './FieldRow';
import { ApiFailure, batchItemImageUrl, setItemDecision } from '../api';
import { evidenceRegions, numberFor, regionFor } from '../evidence';
import { attentionFields, settledFields } from '../triage';
import type { AgentDecision, BatchItem, FieldName } from '../types';

/**
 * What an agent is told when their ruling did not reach the server.
 *
 * Deliberately says the row is UNCHANGED rather than "try again" alone. The button has
 * just sprung back to where it was, and an agent who reads only "could not save" is left
 * unsure whether the queue now holds a half-written decision. Ambiguity about what was
 * recorded is the expensive failure here, not the lost click.
 */
const SAVE_FAILED =
  'That did not save, so this row is unchanged. Press it again — nothing else on this ' +
  'application was affected.';

export default function ItemDetail({
  item,
  onClose,
  onDecisions,
}: {
  item: BatchItem;
  /**
   * Memoise this. The focus effect below depends on it, so a fresh arrow on every render
   * re-runs the effect — and while a job is still running that is every 1.5s poll, each
   * one yanking focus back to the dialog heading in the middle of a decision.
   */
  onClose: () => void;
  /**
   * Hand the server's answer up to the batch screen, which owns the item list.
   *
   * Without this a decision made on a FINISHED job is lost the moment the dialog closes:
   * polling has stopped, so nothing ever refreshes `item.decisions`, and reopening the row
   * seeds from the stale copy the last poll left behind. That is the state almost all
   * triage happens in.
   */
  onDecisions: (decisions: Partial<Record<FieldName, AgentDecision>>) => void;
}) {
  const dialog = useRef<HTMLDivElement | null>(null);
  const heading = useRef<HTMLHeadingElement | null>(null);
  /**
   * Rows that need a human start OPEN, exactly as they do on Verify Now.
   *
   * This was one-at-a-time and closed, which put both decision buttons a click behind
   * "Why this verdict" on every row of every application. Across a queue of three hundred
   * that is a click per row before any judgement happens, and it made the drill-in read
   * as a summary you drill further into rather than as the checklist it is. Settled rows
   * stay shut: they are on screen to be countable, not to be read.
   */
  const [expanded, setExpanded] = useState<ReadonlySet<FieldName>>(
    () => new Set(attentionFields(item.result?.fields ?? []).map((row) => row.field)),
  );
  const [activeField, setActiveField] = useState<FieldName | null>(null);
  const [imageIndex, setImageIndex] = useState(0);
  /**
   * Pictures whose bytes did not arrive. `urlFor` always returns a string, so without
   * this an item image the server cannot serve renders as the browser's broken-image icon
   * beside a complete checklist — which reads as a rendering glitch rather than as
   * "the evidence for these verdicts is not here".
   */
  const [unavailable, setUnavailable] = useState<ReadonlySet<number>>(new Set());

  /**
   * "What the server holds" and "what the agent just clicked" are different facts, and
   * the screen has to be able to tell them apart.
   *
   *   item.decisions  the server's answer, owned by the batch screen. It is NOT copied
   *                   into state here: a local copy is exactly how a decision made after
   *                   the job finished got stranded in a closed dialog. The parent keeps
   *                   every PATCH response, so it survives a close, a reopen, and a poll
   *                   that had already left before the write.
   *   pending         the optimistic value for a row whose write is in flight, so
   *                   pressing a button feels immediate on a queue worked at speed.
   *   problem         the row whose write failed. Clearing `pending` on failure drops the
   *                   display back to the server's value, which is the truth, and this
   *                   says why.
   */
  const [pending, setPending] = useState<Partial<Record<FieldName, AgentDecision | null>>>({});
  const [problem, setProblem] = useState<Partial<Record<FieldName, string>>>({});

  /**
   * One counter per row, so a slow write cannot overwrite a fast one that followed it.
   * Agree-then-disagree in quick succession is two requests, and without this the row
   * ends up showing whichever the network happened to deliver last.
   */
  const writes = useRef<Partial<Record<FieldName, number>>>({});

  const decisionFor = (field: FieldName): AgentDecision | null =>
    field in pending ? (pending[field] ?? null) : (item.decisions?.[field] ?? null);

  const decide = useCallback(
    (field: FieldName, next: AgentDecision | null) => {
      const seq = (writes.current[field] ?? 0) + 1;
      writes.current[field] = seq;

      setPending((current) => ({ ...current, [field]: next }));
      setProblem((current) => {
        if (!(field in current)) return current;
        const rest = { ...current };
        delete rest[field];
        return rest;
      });

      const settle = (apply: () => void) => {
        // A later click on the same row has already won. Landing this response would
        // undo a ruling the agent made after this one.
        if (writes.current[field] !== seq) return;
        apply();
        setPending((current) => {
          const rest = { ...current };
          delete rest[field];
          return rest;
        });
      };

      // Deliberately NOT aborted when the dialog closes. The agent asked for this row to
      // be recorded; cancelling the write because they moved on to the next application
      // would discard the very thing they clicked for. The response lands on an unmounted
      // component, which React treats as a no-op, and the next poll carries the truth.
      void setItemDecision(item.job_id, item.item_id, field, next)
        .then((updated) => settle(() => onDecisions(updated.decisions)))
        .catch((error: unknown) =>
          settle(() =>
            setProblem((current) => ({
              ...current,
              [field]:
                error instanceof ApiFailure && error.detail.message
                  ? `${error.detail.message} This row is unchanged.`
                  : SAVE_FAILED,
            })),
          ),
        );
    },
    [item.item_id, item.job_id, onDecisions],
  );

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

  /**
   * Per picture, not per item.
   *
   * This asked whether ANY image carried a server URL, which on a two-sided label where
   * the server named the front and not the back declared both exact — and then drew the
   * back's outlines over the un-deskewed original with no warning at all. The question is
   * only ever about the picture currently on screen.
   */
  const geometryIsApproximate = !reportFor(imageIndex)?.url;

  const labelFor = (index: number): string => {
    const role = reportFor(index)?.role;
    return role ? `Label picture — ${role}` : `Label picture ${index + 1}`;
  };

  /**
   * Highlight a row's region, switching pictures if it lives on the other one.
   *
   * `FieldRow` prints "Outlined as 2 on the picture" for any row that has a bbox, but the
   * overlay only draws regions belonging to the picture on screen. On a front/back pair
   * that sentence could name an outline the agent was looking straight past, with nothing
   * to say it was on the other face. Following the region is the only reading of that
   * sentence that is true.
   */
  const toggle = (field: FieldName) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(field)) next.delete(field);
      else next.add(field);
      return next;
    });
  };

  const activate = (field: FieldName | null) => {
    setActiveField(field);
    const region = regionFor(regions, field);
    if (region) setImageIndex(region.imageIndex);
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
          <AggregateBanner aggregate={result.aggregate} fields={result.fields} />
        ) : null}

        {/* The picture beside the checklist, exactly as Verify Now arranges them. When the
            item has no picture at all the column is dropped rather than filled with an
            apology — the grid collapses and the remaining column takes the whole width,
            which is what the dialog did before the panel existed.

            **A failed item gets its picture too, with nothing drawn on it.** It used to
            get a paragraph telling the agent to go and check this one on the Verify now
            screen — that is, to re-upload a file the server is already holding, to look at
            a label it could have shown them here. There are no verdicts to cite and so no
            outlines; the picture is just the label, which is the one thing an agent whose
            check did not run still wants. */}
        <div className="drawer__columns" data-evidence={pictureCount > 0 ? 'true' : 'false'}>
          {pictureCount > 0 ? (
            <div className="drawer__evidence">
              <EvidenceOverlay
                imageUrl={unavailable.has(imageIndex) ? null : urlFor(imageIndex)}
                imageIndex={imageIndex}
                imageLabel={labelFor(imageIndex)}
                regions={regions}
                activeField={activeField}
                onActivateField={activate}
                geometryIsApproximate={geometryIsApproximate}
                qualityNote={qualityNote}
                onImageError={() =>
                  setUnavailable((current) => new Set(current).add(imageIndex))
                }
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
            {result ? (
              <>
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
                    {attentionFields(result.fields).map((field) => (
                      <FieldRow
                        key={field.field}
                        result={field}
                        commodity={commodity}
                        variant="attention"
                        // From the regions, NEVER from the position in this list. The
                        // builder skips rows with no bbox, so a `missing` warning — which
                        // sorts first — used to shift every following number by one and
                        // point "outlined as 2" at region 1.
                        number={numberFor(regions, field.field)}
                        expanded={expanded.has(field.field)}
                        onToggle={() => toggle(field.field)}
                        onActivate={(on) => activate(on ? field.field : null)}
                        decision={decisionFor(field.field)}
                        onDecide={(decision) => decide(field.field, decision)}
                        decisionProblem={problem[field.field] ?? null}
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
                        expanded={expanded.has(field.field)}
                        onToggle={() => toggle(field.field)}
                        onActivate={(on) => activate(on ? field.field : null)}
                        decision={decisionFor(field.field)}
                        onDecide={(decision) => decide(field.field, decision)}
                        decisionProblem={problem[field.field] ?? null}
                        isFocused={activeField === field.field}
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
                  Nothing here is a finding against the label — no part of it was read.
                  {pictureCount > 0
                    ? ' The label itself is beside this note if you want to look at it. Retry the batch to check it properly.'
                    : ' Retry the batch to check it properly.'}
                </p>
                {item.attempts > 1 ? (
                  <p className="muted">Tried {item.attempts} times.</p>
                ) : null}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
