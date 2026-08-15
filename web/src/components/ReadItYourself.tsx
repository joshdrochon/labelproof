/**
 * What an agent can do about a row the artwork could not answer (UX-6, HITL-2).
 *
 * An Unreadable row is the one verdict that leaves the agent with nothing. Match,
 * Mismatch, Missing and Acceptable variation all end in a decision they can make from the
 * screen. Unreadable ends in "we did not verify this".
 *
 * **The correction that produced this file's current shape.** The first version offered
 * "Type what the bottle says" and justified it in a comment claiming the bottle was on
 * the agent's desk. That was invented. `PRD.md:145` says what actually happens: *"Today
 * the fallback is reject-and-request-a-better-image."* A TTB agent reviews **submitted
 * artwork**, not a physical bottle — so an agent who cannot read the image has no second
 * source, and a box inviting them to type a value would be inviting a guess, or a copy of
 * the application across into the evidence column. That is the false-pass path everything
 * else in this product is built to prevent, with the agent's own name on it.
 *
 * So the primary action is now the real fallback: request a better image. It sends
 * nothing — the app never contacts applicants (SCOPE-3) — it writes the plain-language
 * reason the agent can paste into their own correspondence.
 *
 * The second action is kept, narrowed, and renamed. A human zooming into artwork the
 * model could not read is a real event: the model says unreadable, the agent looks
 * harder and can make it out. Refusing to record that sends them to a sticky note. But
 * it is now explicitly **what you can read on the artwork**, never "what the bottle
 * says", because those are different claims and only one of them is available.
 *
 * **Neither action makes the row a Match.** The verdict stays Unreadable, the chip does
 * not change, and the report says the value was entered by a person and not verified
 * against the image.
 */

import { useEffect, useId, useRef, useState } from 'react';

import type { AgentEntry, FieldResult } from '../types';
import { fieldLabel } from '../copy';

/**
 * Loose on purpose: case, spacing and punctuation all dropped, so `750mL`, `750 mL` and
 * `750-ML` are one value.
 *
 * A courtesy comparison for a person who has just read the artwork, NOT the matching
 * engine. `api/rules/normalize.py` owns matching, and reimplementing any part of it here
 * would create a second answer that drifts from the one the verdicts are computed with.
 *
 * Dropping spaces rather than collapsing them slightly over-matches — `OldTom` reads as
 * `Old Tom`. Safe here for one specific reason: this comparison can never produce a pass.
 * The row stays Unreadable whatever it says and the caveat under it is unconditional.
 * What over-matching avoids is telling an agent that `750mL` and `750 mL` "do not look
 * the same", which is how a flag teaches people to ignore flags.
 */
export function agreesWithApplication(typed: string, expected: string | null): boolean {
  if (!expected) return false;
  const flatten = (value: string) => value.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
  return flatten(typed) === flatten(expected) && flatten(typed).length > 0;
}

/** The sentence an agent can paste into a request to the applicant. */
export function retakeRequest(result: FieldResult): string {
  return (
    `The submitted artwork could not be read clearly enough to verify ` +
    `${fieldLabel(result.field).toLowerCase()}. Please supply a sharper image in which ` +
    `this element is fully within the frame and legible.`
  );
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
  /** Back to the upload step, keeping the application details. */
  onRetake: () => void;
}) {
  const [open, setOpen] = useState(entry !== null);
  const [showRequest, setShowRequest] = useState(false);
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
      <h3 className="detail__heading">Nothing was verified for this row</h3>
      <p className="detail__text">
        The artwork could not be read here, so this row is neither right nor wrong yet.
      </p>

      <div className="unread__choices">
        <button
          type="button"
          className="btn btn--quiet"
          onClick={() => setShowRequest((current) => !current)}
        >
          {showRequest ? 'Hide the request' : 'Request a better image'}
        </button>
        <button type="button" className="btn btn--quiet" onClick={onRetake}>
          Upload a different image
        </button>
        {!open ? (
          <button type="button" className="btn btn--quiet" onClick={() => setOpen(true)}>
            I can read it on the artwork
          </button>
        ) : null}
      </div>

      {showRequest ? (
        <div className="unread__request">
          <p className="unread__label">
            Wording you can paste into your reply to the applicant:
          </p>
          <p className="unread__request-text">{retakeRequest(result)}</p>
          <p className="unread__entered-caveat">
            Nothing is sent from here. This screen never contacts an applicant.
          </p>
        </div>
      ) : null}

      {open ? (
        <div className="unread__form">
          <label className="unread__label" htmlFor={inputId}>
            What does the artwork say for this?
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
            // Never prefilled from the application. A box that arrives already holding
            // the expected answer invites agreement with it, and agreement is the finding.
            placeholder="Only what you can actually read on the image"
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
      ) : null}

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
            Recorded as read by you from the artwork. This row is still Unreadable —
            nothing has been verified automatically, and the printout will say so.
          </p>
        </div>
      ) : null}
    </div>
  );
}
