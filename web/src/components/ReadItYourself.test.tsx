/**
 * The Unreadable row an agent can act on (UX-6, HITL-2).
 *
 * The assertion that matters most here is a negative one: typing a value must not turn
 * the row into a Match. Everything else on this screen is a reading task; this is the one
 * place an agent can put their own words into the record, and a tool for federal
 * compliance review must never let that become evidence about what the LABEL says.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ReadItYourself, { agreesWithApplication, retakeRequest } from './ReadItYourself';
import { fieldResult } from '../testing';
import type { AgentEntry } from '../types';

function setup(entry: AgentEntry | null = null, expected = '750 mL') {
  const onEnter = vi.fn();
  const onRetake = vi.fn();
  const result = fieldResult('net_contents', 'unreadable', {
    extracted: null,
    expected,
    confidence: 0,
  });
  const user = userEvent.setup();
  render(
    <ReadItYourself result={result} entry={entry} onEnter={onEnter} onRetake={onRetake} />,
  );
  return { user, onEnter, onRetake };
}

describe('agreement is loose, and is not the matching engine', () => {
  it('ignores case, spacing and punctuation', () => {
    expect(agreesWithApplication('750 ML', '750 mL')).toBe(true);
    expect(agreesWithApplication('750mL', '750 mL')).toBe(true);
    expect(agreesWithApplication(' 750-mL ', '750 mL')).toBe(true);
  });

  it('does not call a different value the same', () => {
    expect(agreesWithApplication('375 mL', '750 mL')).toBe(false);
  });

  it('never agrees with nothing', () => {
    // Empty on both sides is the absence of a comparison, not a match. Returning true
    // here would mark a row the agent left blank as agreeing with an empty application.
    expect(agreesWithApplication('', null)).toBe(false);
    expect(agreesWithApplication('', '')).toBe(false);
    expect(agreesWithApplication('750 mL', null)).toBe(false);
  });
});

describe('what the panel offers', () => {
  it('leads with requesting a better image, which is the real fallback', () => {
    // PRD.md:145 — "Today the fallback is reject-and-request-a-better-image." A TTB
    // agent reviews submitted artwork and has no bottle to consult, so this is the
    // action that matches what they can actually do.
    setup();
    expect(
      screen.getByRole('button', { name: /request a better image/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /upload a different image/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /i can read it on the artwork/i }),
    ).toBeInTheDocument();
  });

  it('never invites the agent to read a bottle they do not have', () => {
    // The wording this replaced said "Type what the bottle says", justified by a comment
    // claiming the bottle was on the agent's desk. That was invented, and it pointed the
    // agent at a source they do not have — which leaves guessing, or copying the
    // application across into the evidence column.
    setup();
    expect(screen.queryByText(/bottle/i)).not.toBeInTheDocument();
  });

  it('offers wording to paste, and sends nothing', async () => {
    const { user } = setup();
    await user.click(screen.getByRole('button', { name: /request a better image/i }));

    expect(screen.getByText(/please supply a sharper image/i)).toBeInTheDocument();
    expect(screen.getByText(/never contacts an applicant/i)).toBeInTheDocument();
  });

  it('names the field in the request, so the applicant knows what to reshoot', () => {
    const request = retakeRequest(
      fieldResult('net_contents', 'unreadable', { extracted: null, expected: '750 mL' }),
    );
    expect(request.toLowerCase()).toContain('net contents');
  });

  it('hands back a typed value with its agreement flag', async () => {
    const { user, onEnter } = setup();
    await user.click(screen.getByRole('button', { name: /i can read it on the artwork/i }));
    await user.type(screen.getByRole('textbox'), '750 mL');
    await user.click(screen.getByRole('button', { name: /save what i read/i }));

    expect(onEnter).toHaveBeenCalledWith({ value: '750 mL', agrees: true });
  });

  it('flags a typed value that disagrees with the application', async () => {
    const { user, onEnter } = setup(null, '750 mL');
    await user.click(screen.getByRole('button', { name: /i can read it on the artwork/i }));
    await user.type(screen.getByRole('textbox'), '375 mL');
    await user.click(screen.getByRole('button', { name: /save what i read/i }));

    expect(onEnter).toHaveBeenCalledWith({ value: '375 mL', agrees: false });
  });

  it('never prefills the box with the application value', async () => {
    // A box that arrives holding the expected answer invites agreement with it, and
    // agreement is the finding. The agent has to read the artwork.
    const { user } = setup(null, '750 mL');
    await user.click(screen.getByRole('button', { name: /i can read it on the artwork/i }));
    expect(screen.getByRole('textbox')).toHaveValue('');
  });

  it('treats a blank entry as no entry rather than as an empty reading', async () => {
    const { user, onEnter } = setup();
    await user.click(screen.getByRole('button', { name: /i can read it on the artwork/i }));
    await user.type(screen.getByRole('textbox'), '   ');
    await user.click(screen.getByRole('button', { name: /save what i read/i }));

    expect(onEnter).toHaveBeenCalledWith(null);
  });

  it('goes back for a different image without recording anything', async () => {
    const { user, onEnter, onRetake } = setup();
    await user.click(screen.getByRole('button', { name: /upload a different image/i }));

    expect(onRetake).toHaveBeenCalledTimes(1);
    expect(onEnter).not.toHaveBeenCalled();
  });
});

describe('what a typed value does NOT do', () => {
  it('says plainly that the row is still unverified', () => {
    // The load-bearing assertion. A typed value is the agent's reading of the bottle; it
    // is never evidence about what the label image says, and the screen has to say so in
    // words rather than leaving the agent to infer it from an unchanged chip.
    setup({ value: '750 mL', agrees: true });

    expect(screen.getByText(/still Unreadable/i)).toBeInTheDocument();
    expect(
      screen.getByText(/nothing has been verified automatically/i),
    ).toBeInTheDocument();
  });

  it('carries the caveat even when the reading agrees with the application', () => {
    // Agreement is the tempting case: everything looks fine, so the caveat feels like
    // noise. It is exactly then that dropping it would turn an unverified row into an
    // apparent pass.
    setup({ value: '750 mL', agrees: true });

    expect(screen.getByText(/same as the application says/i)).toBeInTheDocument();
    expect(screen.getByText(/still Unreadable/i)).toBeInTheDocument();
  });
});
