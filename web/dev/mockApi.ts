/**
 * Dev-server stand-in for the API, used only by `npm run dev`.
 *
 * This exists so the UI can be built and looked at while `api/` is being written in
 * parallel. It is **not** shipped: `vite build` never includes this file, and there is
 * no switch in the interface that reaches it — the reviewed rule is that the product has
 * no dev toggles, and a fake result the agent could summon by clicking is exactly the
 * false trust signal this product exists to avoid.
 *
 * Point the dev server at the real API instead with:
 *
 *     LABELPROOF_API=http://127.0.0.1:8000 npm run dev
 */

import type { Connect } from 'vite';

const LABEL_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 1000" width="800" height="1000">
  <rect width="800" height="1000" fill="#f4efe3"/>
  <rect x="24" y="24" width="752" height="952" fill="none" stroke="#2b2b28" stroke-width="4"/>
  <text x="400" y="180" text-anchor="middle" font-family="Georgia,serif" font-size="62" fill="#1b1b19">OLD TOM</text>
  <text x="400" y="248" text-anchor="middle" font-family="Georgia,serif" font-size="44" fill="#1b1b19">DISTILLERY</text>
  <text x="400" y="360" text-anchor="middle" font-family="Georgia,serif" font-size="30" fill="#1b1b19">Kentucky Straight</text>
  <text x="400" y="402" text-anchor="middle" font-family="Georgia,serif" font-size="30" fill="#1b1b19">Bourbon Whiskey</text>
  <text x="400" y="520" text-anchor="middle" font-family="Helvetica,Arial" font-size="30" fill="#1b1b19">40% ALC/VOL (80 PROOF)</text>
  <text x="400" y="580" text-anchor="middle" font-family="Helvetica,Arial" font-size="28" fill="#1b1b19">750 ML</text>
  <text x="400" y="660" text-anchor="middle" font-family="Helvetica,Arial" font-size="24" fill="#3a352c">Old Tom Distillery · Bardstown, Kentucky</text>
  <text x="60" y="770" font-family="Helvetica,Arial" font-size="19" font-weight="bold" fill="#1b1b19">Government Warning:</text>
  <text x="60" y="800" font-family="Helvetica,Arial" font-size="17" fill="#1b1b19">(1) According to the Surgeon General, women should not drink</text>
  <text x="60" y="824" font-family="Helvetica,Arial" font-size="17" fill="#1b1b19">alcoholic beverages during pregnancy because of the risk of birth</text>
  <text x="60" y="848" font-family="Helvetica,Arial" font-size="17" fill="#1b1b19">defects. (2) Consumption of alcoholic beverages impairs your</text>
  <text x="60" y="872" font-family="Helvetica,Arial" font-size="17" fill="#1b1b19">ability to drive a car or operate machinery, and may cause</text>
  <text x="60" y="896" font-family="Helvetica,Arial" font-size="17" fill="#1b1b19">health problems.</text>
</svg>`;

const APPLICATION = {
  commodity: 'spirits',
  brand_name: 'OLD TOM DISTILLERY',
  class_type: 'Kentucky Straight Bourbon Whiskey',
  alcohol_content: 45.0,
  net_contents: '750 mL',
  producer_name: 'Old Tom Distillery',
  producer_address: 'Bardstown, Kentucky',
  country_of_origin: null,
  is_import: false,
};

const CANONICAL_WARNING =
  'GOVERNMENT WARNING: (1) According to the Surgeon General, women should not drink ' +
  'alcoholic beverages during pregnancy because of the risk of birth defects. ' +
  '(2) Consumption of alcoholic beverages impairs your ability to drive a car or ' +
  'operate machinery, and may cause health problems.';

const LABEL_WARNING = CANONICAL_WARNING.replace(
  'GOVERNMENT WARNING:',
  'Government Warning:',
);

const RESULT = {
  request_id: 'req_01JDEVSAMPLE0001',
  aggregate: {
    recommendation: 'return_for_correction',
    rationale:
      'Recommend returning this application for correction because the government ' +
      'warning statement does not match the required text. The final decision is yours.',
    driving_field: 'government_warning',
  },
  fields: [
    {
      field: 'government_warning',
      verdict: 'mismatch',
      extracted: LABEL_WARNING,
      expected: CANONICAL_WARNING,
      confidence: 0.96,
      rationale:
        'The wording is correct, but the words "GOVERNMENT WARNING:" are printed as ' +
        '"Government Warning:". The regulation requires that part in capital letters.',
      tier: 1,
      evidence: { image_index: 0, bbox: { x0: 0.06, y0: 0.74, x1: 0.94, y1: 0.91 } },
      findings: [
        {
          code: 'warning_header_not_caps',
          message:
            'The words "GOVERNMENT WARNING" must be in capital letters and bold.',
          citation: '27 CFR 16.22',
          severity: 'violation',
        },
      ],
    },
    {
      field: 'alcohol_content',
      verdict: 'mismatch',
      extracted: '40% ALC/VOL (80 PROOF)',
      expected: '45',
      confidence: 0.98,
      rationale:
        'The application states 45 percent. The label shows 40 percent, and 80 proof ' +
        'agrees with the 40 on the label rather than with the application.',
      tier: 1,
      evidence: { image_index: 0, bbox: { x0: 0.2, y0: 0.49, x1: 0.8, y1: 0.535 } },
      findings: [],
    },
    {
      field: 'brand_name',
      verdict: 'acceptable_variation',
      extracted: 'OLD TOM DISTILLERY',
      expected: 'OLD TOM DISTILLERY',
      confidence: 0.97,
      rationale:
        'The label sets the name across two lines. The words are the same as the ' +
        'application, so this is a layout difference rather than a different name.',
      tier: 2,
      evidence: { image_index: 0, bbox: { x0: 0.18, y0: 0.13, x1: 0.82, y1: 0.26 } },
      findings: [],
    },
    {
      field: 'class_type',
      verdict: 'match',
      extracted: 'Kentucky Straight Bourbon Whiskey',
      expected: 'Kentucky Straight Bourbon Whiskey',
      confidence: 0.99,
      rationale: 'The label and the application say the same thing.',
      tier: 1,
      evidence: { image_index: 0, bbox: { x0: 0.2, y0: 0.33, x1: 0.8, y1: 0.41 } },
      findings: [],
    },
    {
      field: 'net_contents',
      verdict: 'match',
      extracted: '750 ML',
      expected: '750 mL',
      confidence: 0.99,
      rationale: 'Same quantity, written in capitals on the label.',
      tier: 1,
      evidence: { image_index: 0, bbox: { x0: 0.35, y0: 0.555, x1: 0.65, y1: 0.595 } },
      findings: [],
    },
    {
      field: 'producer',
      verdict: 'match',
      extracted: 'Old Tom Distillery · Bardstown, Kentucky',
      expected: 'Old Tom Distillery, Bardstown, Kentucky',
      confidence: 0.95,
      rationale: 'Same name and same city and state.',
      tier: 1,
      evidence: { image_index: 0, bbox: { x0: 0.12, y0: 0.64, x1: 0.88, y1: 0.68 } },
      findings: [],
    },
    {
      field: 'country_of_origin',
      verdict: 'not_applicable',
      extracted: null,
      expected: null,
      confidence: 1,
      rationale: 'The application is not an import, so no country of origin is required.',
      tier: null,
      evidence: null,
      findings: [],
    },
  ],
  images: [
    {
      index: 0,
      role: 'front',
      quality: {
        blur: 0.91,
        exposure: 0.94,
        glare: 0.03,
        skew_deg: 0.4,
        resolution_ok: true,
        verdict: 'ok',
        reason: null,
      },
    },
  ],
  timings_ms: {
    ingest: 38,
    quality: 16,
    preprocess: 118,
    extract: 2410,
    compare: 2,
    adjudicate: 190,
    total: 2774,
  },
  cost: { input_tokens: 9840, output_tokens: 1120, usd: 0.0772 },
};

function send(res: Parameters<Connect.NextHandleFunction>[1], body: unknown): void {
  res.setHeader('Content-Type', 'application/json');
  res.end(JSON.stringify(body));
}

/**
 * Answers `/sample`, `/sample/images/{name}` and `/verify`, in the same shapes as
 * `api/routes/`. Everything else falls through to Vite.
 */
export const mockApi: Connect.NextHandleFunction = (req, res, next) => {
  const url = (req.url ?? '').split('?')[0];

  if (url === '/sample/images/old_tom_front.svg') {
    res.setHeader('Content-Type', 'image/svg+xml');
    res.end(LABEL_SVG);
    return;
  }

  if (url === '/sample') {
    send(res, {
      application: APPLICATION,
      note: 'Dev-server stand-in. The real endpoint serves the generated label pair.',
      images: [
        {
          index: 0,
          role: 'front',
          filename: 'old_tom_front.svg',
          media_type: 'image/svg+xml',
          url: '/sample/images/old_tom_front.svg',
        },
      ],
    });
    return;
  }

  if (url === '/prepare' && req.method === 'POST') {
    // The stand-in has to speak this too. Without it every dev upload silently loses the
    // head start, which is invisible precisely where it would be looked at.
    req.on('data', () => undefined);
    req.on('end', () => {
      setTimeout(
        () =>
          send(res, {
            prepared: true,
            token: 'dev-prepared-token',
            read_ms: 5800,
            expires_in_s: 600,
            images: [],
          }),
        1800,
      );
    });
    return;
  }

  if (url === '/verify' && req.method === 'POST') {
    // Drain the upload, wait long enough to see the stage narration, then answer.
    req.on('data', () => undefined);
    req.on('end', () => {
      setTimeout(() => send(res, RESULT), 2200);
    });
    return;
  }

  next();
};
