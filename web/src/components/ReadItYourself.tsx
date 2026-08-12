/**
 * What an agent can do about a row the picture could not answer (UX-6, HITL-2).
 *
 * An Unreadable row is the one verdict that leaves the agent with nothing. Match,
 * Mismatch, Missing and Acceptable variation all end in a decision they can make from the
 * screen. Unreadable ends in "we did not verify this", and until now the screen stopped
 * there and left them to work out the rest.
 *
 * Two ways forward, and they are genuinely different:
 *
 *   **Read it off the bottle.** The bottle is on the agent's desk. Typing what it says is
 *   faster than re-photographing it, and it is what they would have done before this tool
 *   existed.
 *
 *   **Take a better picture.** The right answer when the image is the problem and the
 *   bottle is not to hand — a glare blowout, a cropped frame, a photograph that is out of
 *   focus.
 *
 * **What typing a value does NOT do: it does not make the row a Match.**
 *
 * This is the load-bearing decision in the component and it is worth being blunt about.
 * The verdict stays Unreadable, the chip does not change, and the report says the value
 * was entered by a person and not verified against the image. A tool for federal
 * compliance review must never let an agent's typing become evidence that the *label*
 * says something — that is a false pass with the agent's own name on it, and it would be
 * the easiest one in the product to produce by accident.
 *
 * What it does do is tell them whether what they read agrees with the application, which
 * is the actual question in front of them. That comparison is advisory, it is labelled
 * advisory, and it is the agent's to act on.
 */

import { useEffect, useId, useRef, useState } from 'react';

import type { AgentEntry, FieldResult } from '../types';

/**
 * Loose on purpose: case, spacing and punctuation all dropped entirely, so `750mL`,
 * `750 mL` and `750-ML` are one value.
 *
 * This is a courtesy comparison for a human holding the bottle, NOT the matching engine.
 * `api/rules/normalize.py` owns matching, and reimplementing any part of it here would
 * create a second answer that drifts from the one the verdicts are computed with.
 *
 * Dropping spaces rather than collapsing them slightly over-matches — `OldTom` reads as
 * `Old Tom`. That direction is chosen deliberately and it is safe here for one specific
 * reason: this comparison can never produce a pass. The row stays Unreadable whatever it
 * says, and the caveat under it is unconditional. What over-matching avoids is telling an
 * agent that `750mL` and `750 mL` "do not look the same" — a false alarm on an identical
 * value, which is how a flag teaches people to ignore flags.
 *
 * If this ever gained the power to settle a row, invert it: under-match and make the
 * agent look.
 */
export function agreesWithApplication(typed: string, expected: string | null): boolean {
  if (!expected) return false;
  const flatten = (value: string) => value.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
  return flatten(typed) === flatten(expected) && flatten(typed).length > 0;
}

export default function ReadItYourself({
  result,
  entry,
  onEnter,
  onRetake,
}: {
  result: FieldResult;
  entry: AgentEntry | null;
  onEnter: (entry: AgentEntry | null) => void;
  onRetake: () => void;
}) {
  const [open, setOpen] = useState(entry !== null);
  const [text, setText] = useState(entry?.value ?? '');
  const input = useRef<HTMLInputElement | null>(null);
  const inputId = useId();

  useEffect(() => {
    if (open) input.current?.focus();
  }, [open]);

  const save = () => {
    const value = text.trim();
    onEnter(value ? { value, agrees: agreesWithApplication(value, result.expected) } : null);
  };

  return (
    <div className="unread">
      <h4 className="detail__heading">Nothing was verified for this row</h4>
      <p className="detail__text">
        The picture could not be read here, so this row is neither right nor wrong yet.
      </p>

      {!open ? (
        <div className="unread__choices">
          <button type="button" className="btn btn--quiet" onClick={() => setOpen(true)}>
            Type what the bottle says
          </button>
          <button type="button" className="btn btn--quiet" onClick={onRetake}>
            Use a better picture
          </button>
        </div>
      ) : (
        <div className="unread__form">
          <label className="unread__label" htmlFor={inputId}>
            What does the bottle say for this?
          </label>
          <input
            id={inputId}
            ref={input}
            className="unread__input"
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                save();
              }
              if (e.key === 'Escape') {
                e.preventDefault();
                setText(entry?.value ?? '');
                setOpen(entry !== null);
              }
            }}
            // Never prefilled from the application. A box that arrives already holding the
            // expected answer invites agreement with it, and agreement is the finding.
            placeholder="Read it straight off the label"
            autoComplete="off"
          />
          <div className="unread__choices">
            <button type="button" className="btn btn--quiet" onClick={save}>
              Save what I read
            </button>
            <button
              type="button"
              className="btn btn--quiet"
              onClick={() => {
                setText('');
                setOpen(false);
                onEnter(null);
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {entry ? (
        <div className="unread__entered" data-agrees={entry.agrees ? 'true' : 'false'}>
          <p className="unread__entered-value">
            You read: <strong>{entry.value}</strong>
          </p>
          <p className="unread__entered-note">
            {entry.agrees
              ? 'That is the same as the application says.'
              : `The application says "${result.expected ?? '—'}". These do not look the same.`}
          </p>
          <p className="unread__entered-caveat">
            Recorded as read by you. This row is still Unreadable — nothing has been
            verified against the picture, and the printout will say so.
          </p>
        </div>
      ) : null}
    </div>
  );
}
