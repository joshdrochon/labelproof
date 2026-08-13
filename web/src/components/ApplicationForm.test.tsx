/**
 * Reading a typed entry, client side (LP-336).
 *
 * The mirror of `tests/test_entry.py`. Both exist because the server is the authority and
 * the browser is where the agent finds out — a rule enforced only on the server means a
 * round trip and a lost form; a rule enforced only in the browser means `curl` bypasses
 * it. The defect here was neither: the browser took the FIRST number out of the box and
 * sent it, so `45% (Front) / 43% (Back)` was filed as 45 with nothing said.
 */

import { describe, expect, it } from 'vitest';

import {
  EMPTY_DRAFT,
  readAlcoholContent,
  readNetContents,
  validateDraft,
} from './ApplicationForm';

const filled = {
  ...EMPTY_DRAFT,
  brand_name: 'OLD OAK',
  class_type: 'Bourbon',
  net_contents: '750 mL',
  producer_name: 'Old Tom',
  producer_address: 'Bardstown, Kentucky',
};

describe('readAlcoholContent — generous about decoration', () => {
  it.each([
    ['45', 45],
    ['45.0', 45],
    ['45%', 45],
    ['45 %', 45],
    ['45% ABV', 45],
    ['45% Alc./Vol.', 45],
    ['alc. 45% by vol.', 45],
    ['90 proof', 45],
    ['45% Alc./Vol. (90 Proof)', 45],
    ['45,5%', 45.5],
    ['', null],
  ])('reads %s as %s', (typed, expected) => {
    const reading = readAlcoholContent(typed);
    expect('problem' in reading ? reading.problem : reading.value).toBe(expected);
  });
});

describe('readAlcoholContent — strict about ambiguity', () => {
  it('refuses the entry that was silently filed as 45', () => {
    const reading = readAlcoholContent('45% BY VOL. (Front label) / 43% BY VOL. (Back label)');

    expect('problem' in reading).toBe(true);
    // The numbers it saw, because "enter a number" would be false — they did.
    expect('problem' in reading && reading.problem).toContain('43');
    expect('problem' in reading && reading.problem).toContain('45');
  });

  it('refuses an entry with no number rather than sending null', () => {
    expect('problem' in readAlcoholContent('about forty-five')).toBe(true);
  });

  it('refuses a value that is not a percentage', () => {
    expect('problem' in readAlcoholContent('145%')).toBe(true);
  });
});

describe('readNetContents', () => {
  it('refuses two different sizes in the same unit', () => {
    expect(readNetContents('750mL (Front label) / 700 mL (Back label)').problem).toBeTruthy();
  });

  it('accepts one quantity declared in two units', () => {
    // Ordinary and correct on a real label. Refusing it would be the gate firing on
    // good input, which is how a validation rule teaches people to work around it.
    expect(readNetContents('750 mL (25.4 fl oz)').problem).toBeUndefined();
  });
});

describe('validateDraft', () => {
  it('reports the ambiguous alcohol entry against its own box', () => {
    const problems = validateDraft({ ...filled, alcohol_content: '45% / 43%' }, 1);

    expect(problems.alcohol_content).toContain('43');
    expect(problems.brand_name).toBeUndefined();
  });

  it('reports an ambiguous net contents against its own box', () => {
    const problems = validateDraft({ ...filled, net_contents: '750 mL / 700 mL' }, 1);

    expect(problems.net_contents).toBeTruthy();
  });

  it('leaves a blank alcohol box alone', () => {
    // Wine under 14% and malt beverages may omit it entirely (27 CFR 4.36(b)). An empty
    // box is a fact about the filing, not a mistake by the typist.
    expect(validateDraft({ ...filled, alcohol_content: '' }, 1).alcohol_content).toBeUndefined();
  });

  it('accepts a decorated entry without complaint', () => {
    expect(
      validateDraft({ ...filled, alcohol_content: 'alc. 45% by vol.' }, 1).alcohol_content,
    ).toBeUndefined();
  });
});
