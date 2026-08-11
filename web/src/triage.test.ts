/**
 * Display ordering, against `api/rules/aggregate.py`.
 *
 * This is the mechanism behind the visual hierarchy, so it is tested as a rule and not
 * left to whatever order the server happened to send.
 */

import { describe, expect, it } from 'vitest';
import { attentionFields, needsAttention, settledFields, triageOrder } from './triage';
import { fieldResult } from './testing';

const FIELDS = [
  fieldResult('brand_name', 'acceptable_variation'),
  fieldResult('class_type', 'match'),
  fieldResult('alcohol_content', 'mismatch'),
  fieldResult('net_contents', 'match'),
  fieldResult('producer', 'match'),
  fieldResult('country_of_origin', 'not_applicable'),
  fieldResult('government_warning', 'missing'),
];

describe('triage', () => {
  it('treats Match and Not applicable as nothing to do, everything else as attention', () => {
    expect(needsAttention(fieldResult('brand_name', 'match'))).toBe(false);
    expect(needsAttention(fieldResult('country_of_origin', 'not_applicable'))).toBe(false);
    for (const verdict of ['acceptable_variation', 'unreadable', 'mismatch', 'missing'] as const) {
      expect(needsAttention(fieldResult('brand_name', verdict))).toBe(true);
    }
  });

  it('puts the government warning first and the worst next', () => {
    expect(attentionFields(FIELDS).map((row) => row.field)).toEqual([
      'government_warning',
      'alcohol_content',
      'brand_name',
    ]);
  });

  it('keeps the warning ahead of an equally serious row', () => {
    const rows = [
      fieldResult('brand_name', 'missing'),
      fieldResult('government_warning', 'missing'),
    ];
    expect(triageOrder(rows)[0]?.field).toBe('government_warning');
  });

  it('leaves the settled rows in the order of the paper checklist', () => {
    expect(settledFields(FIELDS).map((row) => row.field)).toEqual([
      'class_type',
      'net_contents',
      'producer',
      'country_of_origin',
    ]);
  });

  it('accounts for every row exactly once across the two groups', () => {
    const total = attentionFields(FIELDS).length + settledFields(FIELDS).length;
    expect(total).toBe(FIELDS.length);
  });
});
