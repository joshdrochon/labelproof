/**
 * One line of the checklist — the paper checklist, digitised (UX-5).
 *
 * **This component is where the visual-hierarchy fix lives.** A row renders in one of two
 * weights, decided by `api/rules/aggregate.py`'s `attention_fields()` rule and nothing
 * else:
 *
 *   - `attention` — the agent has to look. Full type size, a heavy rule down the left
 *     edge, the large verdict chip, a number that ties it to an outline on the photo,
 *     and the reasoning already open. It should be impossible to miss.
 *   - `settled` — checked, agrees, no action. Small type, muted ink, no number, no rule,
 *     reasoning collapsed behind one control. Still present, still printable, still
 *     readable — but it does not compete.
 *
 * Five Match rows carrying the same weight as the one Mismatch is exactly what buries a
 * finding, and it is what the mock got wrong.
 */

import type { AgentDecision, Commodity, FieldResult } from '../types';
import { VERDICTS, fieldLabel, referenceCitations } from '../copy';
import VerdictChip from './VerdictCard';
import DiffView, { DiffText, useValueDiff } from './DiffView';

/**
 * Past this, a value stops fitting a table cell and gets a short opening in the row with
 * the whole thing in the panel below. The government warning is the reason this exists —
 * fifty words of legalese in a column turns one row into a page.
 */
const LONG_VALUE = 90;
const CELL_PREVIEW = 48;

interface FieldRowProps {
  result: FieldResult;
  commodity: Commodity;
  variant: 'attention' | 'settled';
  /** Ties the row to the outline on the photo. Only attention rows carry one. */
  number: number | null;
  expanded: boolean;
  onToggle: () => void;
  onActivate: (active: boolean) => void;
  decision: AgentDecision | null;
  onDecide: (decision: AgentDecision | null) => void;
  isFocused: boolean;
}

function preview(value: string | null): string | null {
  if (value === null) return null;
  if (value.length <= CELL_PREVIEW) return value;
  return `${value.slice(0, CELL_PREVIEW).trimEnd()}…`;
}

/** Plain words for how clearly the label read. Never a number, never "confidence". */
function legibilityNote(result: FieldResult): string | null {
  if (result.verdict === 'not_applicable' || result.extracted === null) return null;
  if (result.confidence >= 0.9) return 'This part of the label read clearly.';
  if (result.confidence >= 0.75) return 'This part of the label was a little hard to read.';
  return 'This part of the label was hard to read, so treat the reading with care.';
}

export default function FieldRow({
  result,
  commodity,
  variant,
  number,
  expanded,
  onToggle,
  onActivate,
  decision,
  onDecide,
  isFocused,
}: FieldRowProps) {
  const meta = VERDICTS[result.verdict];
  const attention = variant === 'attention';
  const isLong =
    (result.expected?.length ?? 0) > LONG_VALUE || (result.extracted?.length ?? 0) > LONG_VALUE;
  const [left, right] = useValueDiff(result.expected, result.extracted, attention && !isLong);
  const detailId = `detail-${result.field}`;
  // "Not found on the label" would be a finding. On a Not applicable row it is simply
  // not expected to be there, and saying it the wrong way reads as a problem.
  const emptyExtracted =
    result.verdict === 'not_applicable' ? 'Not required here' : 'Not found on the label';
  const citations = referenceCitations(result.field, commodity);
  const hasRegion = Boolean(result.evidence?.bbox);
  const legibility = legibilityNote(result);

  return (
    <>
      <tr
        className="row"
        data-variant={variant}
        data-verdict={result.verdict}
        data-field={result.field}
        data-focused={isFocused ? 'true' : 'false'}
        data-testid={`row-${result.field}`}
        id={`row-${result.field}`}
        onMouseEnter={() => onActivate(true)}
        onMouseLeave={() => onActivate(false)}
        onFocus={() => onActivate(true)}
        onBlur={() => onActivate(false)}
      >
        <th scope="row" className="cell cell--field">
          {number !== null ? (
            <span className="row__number" aria-hidden="true">
              {number}
            </span>
          ) : null}
          <span className="row__label">{fieldLabel(result.field)}</span>
          {number !== null && hasRegion ? (
            <span className="row__region-note">
              Outlined as {number} on the picture
            </span>
          ) : null}
        </th>

        <td className="cell cell--value">
          <span className="cell__mobile-caption">The application says</span>
          <span className="cell__value">
            {isLong ? (
              <>
                {preview(result.expected) ?? 'Nothing filed'}
                <span className="cell__more">Full wording below</span>
              </>
            ) : (
              <DiffText pieces={left} empty="Nothing filed" />
            )}
          </span>
        </td>

        <td className="cell cell--value">
          <span className="cell__mobile-caption">The label shows</span>
          <span className="cell__value">
            {isLong ? (
              <>
                {preview(result.extracted) ?? emptyExtracted}
                <span className="cell__more">Full wording below</span>
              </>
            ) : (
              <DiffText pieces={right} empty={emptyExtracted} />
            )}
          </span>
        </td>

        <td className="cell cell--verdict">
          <VerdictChip verdict={result.verdict} size={attention ? 'lg' : 'sm'} />
          <button
            type="button"
            className="row__toggle"
            aria-expanded={expanded}
            aria-controls={detailId}
            onClick={onToggle}
          >
            {expanded ? 'Hide the reason' : 'Why this verdict'}
            <span className="row__chevron" aria-hidden="true" data-open={expanded}>
              ▾
            </span>
          </button>
        </td>
      </tr>

      <tr
        className="row__detail-row"
        data-open={expanded ? 'true' : 'false'}
        data-variant={variant}
        hidden={!expanded}
      >
        <td colSpan={4} className="row__detail" id={detailId}>
          <div className="detail">
            <div className="detail__main">
              <h4 className="detail__heading">Why this verdict</h4>
              <p className="detail__text">{result.rationale || meta.meaning}</p>
              {legibility ? <p className="detail__text detail__text--soft">{legibility}</p> : null}

              {isLong ? (
                <DiffView
                  expected={result.expected}
                  extracted={result.extracted}
                  mark={attention}
                />
              ) : null}

              {result.findings.length > 0 ? (
                <ul className="detail__findings">
                  {result.findings.map((finding, index) => (
                    <li key={`${finding.code}-${index}`} className="detail__finding">
                      <span className="detail__finding-text">{finding.message}</span>
                      {finding.citation ? (
                        <span className="detail__citation">{finding.citation}</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}

              <h4 className="detail__heading">What to do</h4>
              <p className="detail__text">{meta.whatToDo}</p>

              {!hasRegion ? (
                <p className="detail__text detail__text--soft">
                  No area is outlined on the picture for this row.
                </p>
              ) : null}

              {citations.length > 0 ? (
                <p className="detail__reference">
                  <span className="detail__reference-label">Reference</span>
                  {citations.join(' · ')}
                </p>
              ) : null}
            </div>

            <div className="detail__decision">
              <span className="detail__decision-label" id={`decide-${result.field}`}>
                This row
              </span>
              <div
                className="detail__decision-buttons"
                role="group"
                aria-labelledby={`decide-${result.field}`}
              >
                <button
                  type="button"
                  className="btn btn--quiet"
                  aria-pressed={decision === 'confirmed'}
                  onClick={() => onDecide(decision === 'confirmed' ? null : 'confirmed')}
                >
                  I agree
                </button>
                <button
                  type="button"
                  className="btn btn--quiet"
                  aria-pressed={decision === 'overridden'}
                  onClick={() => onDecide(decision === 'overridden' ? null : 'overridden')}
                >
                  I disagree
                </button>
              </div>
              {decision ? (
                <p className="detail__decision-state">
                  {decision === 'confirmed'
                    ? 'You agreed with this row.'
                    : 'You disagreed with this row. It will show on the printout.'}
                </p>
              ) : null}
            </div>
          </div>
        </td>
      </tr>
    </>
  );
}
