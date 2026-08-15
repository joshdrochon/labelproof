/**
 * The recommendation banner.
 *
 * Three things and nothing else:
 *
 *   1. **"Recommendation:" then the word.** The prefix is fixed and never removed. The
 *      app advises; the agent determines (HITL-1, SCOPE-3). No sentence in here is an
 *      instruction, and there is no button in here that decides anything.
 *   2. **Which rows need eyes, by name.** "Needs review" on its own sends an agent
 *      hunting down a seven-row table. Naming the rows — and letting them be clicked —
 *      is the difference between a status and a starting point.
 *   3. **How long it took.** Trust in a tool that answers in seconds is partly trust
 *      that it actually did something; the elapsed time is stated plainly (PERF-2).
 */

import type { Aggregate, FieldName, FieldResult } from '../types';
import { RECOMMENDATION_PREFIX, RECOMMENDATIONS, fieldLabel } from '../copy';
import { attentionFields } from '../triage';
import { VerdictGlyph } from './VerdictCard';

export function formatElapsed(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return '';
  if (ms < 1000) return 'in under a second';
  return `in ${(ms / 1000).toFixed(1)} seconds`;
}

interface AggregateBannerProps {
  aggregate: Aggregate;
  fields: FieldResult[];
  /** Wall-clock from submit to answer, measured in the browser. */
  /** How long the agent waited, from pressing the button to this screen. */
  elapsedMs: number;
  /** How long the CHECK took, server-side, including a reading taken before the click. */
  workMs?: number;
  onJumpToField?: (field: FieldName) => void;
}

export default function AggregateBanner({
  aggregate,
  fields,
  elapsedMs,
  workMs,
  onJumpToField,
}: AggregateBannerProps) {
  const meta = RECOMMENDATIONS[aggregate.recommendation];
  const attention = attentionFields(fields);
  // The headline is the WORK, not the wait. When the label was read while the form was
  // being filled, the wait is a fraction of a second and reporting that as "checked in
  // under a second" would describe a six-second model call as instant — the one thing a
  // timing display must never do. The saving is real and is stated as what it is: time
  // that was already being spent on typing.
  const readAhead = typeof workMs === 'number' && workMs - elapsedMs > 500;
  const elapsed = formatElapsed(readAhead ? workMs : elapsedMs);

  return (
    <section
      className="banner"
      data-tone={meta.tone}
      data-recommendation={aggregate.recommendation}
      aria-labelledby="recommendation-heading"
    >
      <div className="banner__mark" aria-hidden="true">
        <VerdictGlyph icon={meta.icon} />
      </div>

      <div className="banner__body">
        <h2 className="banner__heading" id="recommendation-heading">
          <span className="banner__prefix">{RECOMMENDATION_PREFIX}</span>{' '}
          <span className="banner__word">{meta.word}</span>
        </h2>

        {aggregate.rationale ? (
          <p className="banner__rationale">{aggregate.rationale}</p>
        ) : null}

        {attention.length > 0 ? (
          <p className="banner__rows">
            <span className="banner__rows-label">
              {attention.length === 1 ? 'The row to look at:' : 'The rows to look at:'}
            </span>{' '}
            {attention.map((row, index) => (
              <span key={row.field}>
                {index > 0 ? ', ' : ''}
                {onJumpToField ? (
                  <button
                    type="button"
                    className="banner__row-link"
                    onClick={() => onJumpToField(row.field)}
                  >
                    {fieldLabel(row.field)}
                  </button>
                ) : (
                  <span className="banner__row-link">{fieldLabel(row.field)}</span>
                )}
              </span>
            ))}
          </p>
        ) : (
          <p className="banner__rows">
            <span className="banner__rows-label">
              No row needs a second look. Every required field was found and agrees with
              the application.
            </span>
          </p>
        )}
      </div>

      {elapsed ? (
        <p className="banner__elapsed" data-testid="elapsed">
          Checked {elapsed}
          {readAhead ? (
            <>
              {' · '}
              <span className="banner__elapsed-note">
                most of it before you pressed the button
              </span>
            </>
          ) : null}
        </p>
      ) : null}
    </section>
  );
}
