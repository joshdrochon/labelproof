/**
 * Fixture builders for the component tests. Not imported by the app, so it never
 * reaches the bundle.
 */

import type {
  Aggregate,
  Application,
  FieldResult,
  FieldName,
  Recommendation,
  Verdict,
} from './types';

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

export const APPLICATION: Application = {
  commodity: 'spirits',
  brand_name: 'OLD TOM DISTILLERY',
  class_type: 'Kentucky Straight Bourbon Whiskey',
  alcohol_content: 45,
  net_contents: '750 mL',
  producer_name: 'Old Tom Distillery',
  producer_address: 'Bardstown, Kentucky',
  country_of_origin: null,
  is_import: false,
};

/** All seven fields, one needing attention, as a real result would come back. */
export function sevenFields(): FieldResult[] {
  return [
    fieldResult('brand_name', 'match'),
    fieldResult('class_type', 'match'),
    fieldResult('alcohol_content', 'match'),
    fieldResult('net_contents', 'match'),
    fieldResult('producer', 'match'),
    fieldResult('country_of_origin', 'not_applicable', { evidence: null }),
    fieldResult('government_warning', 'mismatch'),
  ];
}

/**
 * What `GET /sample` sends back when the server has already verified the demo.
 *
 * This exists so tests can drive the REAL `VerifyNow` all the way to its checked screen
 * over a stubbed `fetch`, rather than asserting against a hand-built stub of the screen.
 * `loadSample` short-circuits on a payload that already carries `aggregate` and `fields`
 * — the "already verified, render it as it stands" path in `api.ts` — so no image blobs
 * and no `/verify` round trip are needed to reach the state an agent actually reads.
 *
 * One `images` array serves two consumers on purpose, because the server's does too:
 * `sampleImages()` takes the `url` off each entry, and `normalizeResult` takes `index`,
 * `role` and `quality` off the same objects.
 */
export function samplePayload(
  options: {
    fields?: FieldResult[];
    recommendation?: Recommendation;
    pictures?: { role: string; url: string }[];
  } = {},
): Record<string, unknown> {
  const fields = options.fields ?? sevenFields();
  const pictures = options.pictures ?? [{ role: 'front', url: '/sample/images/front.png' }];
  return {
    slug: 'old-tom',
    cases: [
      { slug: 'old-tom', title: 'Old Tom — a clean label', summary: 'Everything agrees.' },
    ],
    application: APPLICATION,
    request_id: 'req_01JSAMPLE',
    aggregate: aggregate({
      recommendation: options.recommendation ?? 'return_for_correction',
      rationale: 'The government warning does not match.',
      driving_field: 'government_warning',
    }),
    fields,
    images: pictures.map((picture, index) => ({
      index,
      role: picture.role,
      filename: `${picture.role}.png`,
      url: picture.url,
      quality: {
        blur: 0.9,
        exposure: 0.9,
        glare: 0.02,
        skew_deg: 0.4,
        resolution_ok: true,
        verdict: 'ok',
        reason: null,
      },
    })),
    timings_ms: {
      ingest: 40, quality: 18, preprocess: 120, extract: 2600,
      compare: 2, adjudicate: null, total: 2780,
    },
    cost: {
      input_tokens: 100, output_tokens: 20, cache_read_tokens: 0,
      cache_creation_tokens: 0, usd: 0.01,
    },
  };
}
