/**
 * Fixture builders for the component tests. Not imported by the app, so it never
 * reaches the bundle.
 */

import type { Aggregate, FieldResult, FieldName, Verdict } from './types';

export function fieldResult(
  field: FieldName,
  verdict: Verdict,
  overrides: Partial<FieldResult> = {},
): FieldResult {
  return {
    field,
    verdict,
    extracted: 'what the label shows',
    expected: 'what the application says',
    confidence: 0.95,
    rationale: 'A plain sentence explaining the verdict.',
    tier: 1,
    evidence: { image_index: 0, bbox: { x0: 0.1, y0: 0.1, x1: 0.5, y1: 0.2 } },
    findings: [],
    ...overrides,
  };
}

export function aggregate(overrides: Partial<Aggregate> = {}): Aggregate {
  return {
    recommendation: 'needs_review',
    rationale: '2 rows need your eyes. Everything else checks out. The final decision is yours.',
    driving_field: 'government_warning',
    ...overrides,
  };
}
