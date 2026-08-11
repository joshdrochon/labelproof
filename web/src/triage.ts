/**
 * Display ordering — a direct port of `api/rules/aggregate.py`.
 *
 * The server already sorts and already knows which rows need a human. This mirrors its
 * two rules so the screen cannot drift from the recommendation it is showing:
 *
 *   1. Worst first. Severity is the same ladder the server uses.
 *   2. The government warning outranks everything at equal severity.
 *
 * `attentionFields()` is the whole answer to the visual-hierarchy problem: rows it
 * returns get weight, rows it does not get quieted.
 */

import type { FieldName, FieldResult, Verdict } from './types';

/** Ascending seriousness. Never displayed — comparison only. */
const SEVERITY: Record<Verdict, number> = {
  not_applicable: 0,
  match: 0,
  acceptable_variation: 1,
  unreadable: 2,
  mismatch: 3,
  missing: 4,
};

/** Canonical field order, matching `FieldName` in `api/models.py`. */
const FIELD_ORDER: FieldName[] = [
  'brand_name',
  'class_type',
  'alcohol_content',
  'net_contents',
  'producer',
  'country_of_origin',
  'government_warning',
];

export function severityOf(verdict: Verdict): number {
  return SEVERITY[verdict] ?? 0;
}

/** True when a human has to look at this row. */
export function needsAttention(result: FieldResult): boolean {
  return severityOf(result.verdict) > 0;
}

/** Warning first, then most serious, then canonical field order. */
export function triageOrder(results: FieldResult[]): FieldResult[] {
  const rank = (r: FieldResult): [number, number, number] => [
    r.field === 'government_warning' ? 0 : 1,
    -severityOf(r.verdict),
    Math.max(0, FIELD_ORDER.indexOf(r.field)),
  ];
  return [...results].sort((a, b) => {
    const ka = rank(a);
    const kb = rank(b);
    for (let i = 0; i < ka.length; i += 1) {
      const d = (ka[i] as number) - (kb[i] as number);
      if (d !== 0) return d;
    }
    return 0;
  });
}

/** The subset an agent actually has to look at, in triage order. */
export function attentionFields(results: FieldResult[]): FieldResult[] {
  return triageOrder(results.filter(needsAttention));
}

/** Everything else — checked, matching, no action. Kept in canonical order. */
export function settledFields(results: FieldResult[]): FieldResult[] {
  const keep = new Set(results.filter(needsAttention));
  return results
    .filter((r) => !keep.has(r))
    .sort(
      (a, b) =>
        Math.max(0, FIELD_ORDER.indexOf(a.field)) -
        Math.max(0, FIELD_ORDER.indexOf(b.field)),
    );
}
