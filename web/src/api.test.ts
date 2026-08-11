/**
 * The parse boundary.
 *
 * These are contract tests in the client's own terms: the shapes `api/models.py`
 * serialises must survive the trip, and anything the UI cannot safely assume — a bbox
 * that is not there, a field it does not recognise — must come out absent rather than
 * invented.
 */

import { describe, expect, it } from 'vitest';
import { normalizeResult } from './api';

const BASE = {
  request_id: 'req_01J',
  aggregate: {
    recommendation: 'return_for_correction',
    rationale: 'Recommend returning this application for correction.',
    driving_field: 'government_warning',
  },
  fields: [
    {
      field: 'government_warning',
      verdict: 'mismatch',
      extracted: 'Government Warning: …',
      expected: 'GOVERNMENT WARNING: …',
      confidence: 0.96,
      rationale: 'The header is not in capitals.',
      tier: 1,
      evidence: { image_index: 0, bbox: { x0: 0.1, y0: 0.7, x1: 0.9, y1: 0.9 } },
      findings: [
        {
          code: 'warning_header_not_caps',
          message: 'Must be capitals.',
          citation: '27 CFR 16.22',
          severity: 'violation',
        },
      ],
    },
  ],
  images: [
    {
      index: 0,
      role: 'front',
      quality: {
        blur: 0.9,
        exposure: 0.9,
        glare: 0.02,
        skew_deg: 1.1,
        resolution_ok: true,
        verdict: 'ok',
        reason: null,
      },
    },
  ],
  timings_ms: { ingest: 40, quality: 18, preprocess: 120, extract: 2600, compare: 2, adjudicate: 0, total: 2780 },
  cost: { input_tokens: 9840, output_tokens: 1120, usd: 0.0772 },
};

describe('normalizeResult', () => {
  it('reads the bbox as named corners, which is what the server sends', () => {
    const result = normalizeResult(BASE);
    expect(result.fields[0]?.evidence?.bbox).toEqual({
      x0: 0.1,
      y0: 0.7,
      x1: 0.9,
      y1: 0.9,
    });
  });

  it('keeps a missing bbox missing rather than filling one in', () => {
    const result = normalizeResult({
      ...BASE,
      fields: [{ ...BASE.fields[0], evidence: { image_index: 0, bbox: null } }],
    });
    expect(result.fields[0]?.evidence?.bbox).toBeNull();
  });

  it('keeps an absent evidence block absent', () => {
    const result = normalizeResult({
      ...BASE,
      fields: [{ ...BASE.fields[0], evidence: null }],
    });
    expect(result.fields[0]?.evidence).toBeNull();
  });

  it('carries the three recommendation values through unchanged', () => {
    for (const value of ['ready_to_approve', 'needs_review', 'return_for_correction']) {
      const result = normalizeResult({
        ...BASE,
        aggregate: { ...BASE.aggregate, recommendation: value },
      });
      expect(result.aggregate.recommendation).toBe(value);
    }
  });

  it('falls back to Needs review rather than inventing an outcome it does not know', () => {
    const result = normalizeResult({
      ...BASE,
      aggregate: { ...BASE.aggregate, recommendation: 'something_new' },
    });
    expect(result.aggregate.recommendation).toBe('needs_review');
  });

  it('refuses a response it cannot read instead of rendering an empty checklist', () => {
    expect(() => normalizeResult({ nope: true })).toThrowError(/could not be read/i);
  });

  it('keeps findings and their citations', () => {
    const result = normalizeResult(BASE);
    expect(result.fields[0]?.findings[0]?.citation).toBe('27 CFR 16.22');
  });
});
