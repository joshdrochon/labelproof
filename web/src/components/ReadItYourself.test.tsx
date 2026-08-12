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

import ReadItYourself, { agreesWithApplication } from './ReadItYourself';
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
  it('offers both ways forward before either is chosen', async () => {
    setup();
    expect(screen.getByRole('button', { name: /type what the bottle says/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /better picture/i })).toBeInTheDocument();
  });

  it('hands back a typed value with its agreement flag', async () => {
    const { user, onEnter } = setup();
    await user.click(screen.getByRole('button', { name: /type what the bottle says/i }));
    await user.type(screen.getByRole('textbox'), '750 mL');
    await user.click(screen.getByRole('button', { name: /save what i read/i }));

    expect(onEnter).toHaveBeenCalledWith({ value: '750 mL', agrees: true });
  });

  it('flags a typed value that disagrees with the application', async () => {
    const { user, onEnter } = setup(null, '750 mL');
    await user.click(screen.getByRole('button', { name: /type what the bottle says/i }));
    await user.type(screen.getByRole('textbox'), '375 mL');
    await user.click(screen.getByRole('button', { name: /save what i read/i }));

    expect(onEnter).toHaveBeenCalledWith({ value: '375 mL', agrees: false });
  });

  it('never prefills the box with the application value', async () => {
    // A box that arrives holding the expected answer invites agreement with it, and
    // agreement is the finding. The agent has to read the bottle.
    const { user } = setup(null, '750 mL');
    await user.click(screen.getByRole('button', { name: /type what the bottle says/i }));
    expect(screen.getByRole('textbox')).toHaveValue('');
  });

  it('treats a blank entry as no entry rather than as an empty reading', async () => {
    const { user, onEnter } = setup();
    await user.click(screen.getByRole('button', { name: /type what the bottle says/i }));
    await user.type(screen.getByRole('textbox'), '   ');
    await user.click(screen.getByRole('button', { name: /save what i read/i }));

    expect(onEnter).toHaveBeenCalledWith(null);
  });

  it('asks for a better picture without recording anything', async () => {
    const { user, onEnter, onRetake } = setup();
    await user.click(screen.getByRole('button', { name: /better picture/i }));

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
    expect(screen.getByText(/nothing has been verified against the picture/i)).toBeInTheDocument();
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
