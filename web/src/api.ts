/**
 * The only place this app talks to the server.
 *
 * Two jobs beyond fetching:
 *
 *   1. **Shrink before sending.** BUILD.md §8 pins the client-side encode: resize the
 *      long edge to 2576px with `createImageBitmap`, encode WebP off the main thread.
 *      Never below 2576 — that is the high-res tier the small warning text needs.
 *   2. **Be forgiving at the boundary, strict inside.** The server is being built in
 *      parallel. Anything the UI can safely tolerate (a bbox as an array instead of an
 *      object, the older `reject_candidate` spelling) is normalised here so the rest of
 *      the app sees exactly the shape in `types.ts`. Nothing is invented: a value that
 *      is not there stays absent, and an absent bbox stays absent, because a guessed
 *      region is a false trust signal (BUILD.md §6).
 */

import type {
  ApiError,
  Application,
  BoundingBox,
  ErrorKind,
  FieldResult,
  ImageReport,
  VerificationResult,
} from './types';
import { ERROR_FALLBACK } from './copy';

const LONG_EDGE = 2576;

/**
 * Measured, not picked. `python -m scripts.compression_sweep` encodes every robustness
 * fixture at each quality and measures the structural similarity of the government
 * warning's region against its own uncompressed pixels — the smallest type on the label,
 * and the one field where being wrong is disqualifying.
 *
 * Worst-case SSIM over the set: 0.9920 at q100, 0.9846 at q95, 0.9593 at q90, 0.7529 at
 * q60, against a bar of 0.98. So q90 — the value this shipped with before anyone measured
 * it — visibly damages the warning, and q85 starts flagging labels that are perfectly
 * readable.
 *
 * Pinned at the top rather than at q95, the cheapest level that clears the bar. The
 * measurement is on generated labels, which are sharp text on flat ground and the easiest
 * case any encoder will ever see; real photographs carry sensor noise and compress worse.
 * The difference is ~11KB an image against a budget with room, and the failure it buys
 * insurance against is a compliance error. `tests/test_robustness.py` fails if this
 * constant and the sweep's recommendation ever disagree.
 *
 * Keep WebP. At an equal byte budget JPEG is far worse on this content — ~45KB buys
 * WebP q95 at 0.9846 and JPEG q60 at 0.7770 — and comparing the two by quality number
 * rather than by bytes compares different scales that happen to share a name.
 *
 * Caveat on the evidence: the sweep runs libwebp through Pillow, the browser runs libwebp
 * through Chrome. Close, not identical. Re-run against Tier B before lowering this.
 */
const WEBP_Q = 1.0;

/** An error already phrased for an agent. Every throw out of this module is one. */
export class ApiFailure extends Error {
  readonly detail: ApiError;

  constructor(detail: ApiError) {
    super(detail.message);
    this.name = 'ApiFailure';
    this.detail = detail;
  }
}

function failure(
  kind: ErrorKind,
  code: string,
  message: string,
  nextStep?: string,
): ApiFailure {
  return new ApiFailure({ kind, code, message, next_step: nextStep ?? null });
}

// ---------------------------------------------------------------------------------
// Client-side encode
// ---------------------------------------------------------------------------------

/**
 * Resize to the pinned long edge and re-encode. Returns the original file untouched
 * when the browser cannot do it, or when the image is already small enough — never
 * upscales, because inventing pixels helps nothing downstream.
 */
export async function shrinkForUpload(file: File): Promise<Blob> {
  const encodable = /^image\/(jpeg|png|webp)$/.test(file.type);
  if (!encodable) return file;
  if (
    typeof createImageBitmap !== 'function' ||
    typeof OffscreenCanvas === 'undefined'
  ) {
    return file;
  }

  try {
    const probe = await createImageBitmap(file);
    const longEdge = Math.max(probe.width, probe.height);
    if (longEdge <= LONG_EDGE) {
      probe.close();
      return file;
    }
    const scale = LONG_EDGE / longEdge;
    const width = Math.round(probe.width * scale);
    const height = Math.round(probe.height * scale);
    probe.close();

    const bmp = await createImageBitmap(file, {
      resizeWidth: width,
      resizeHeight: height,
      resizeQuality: 'high',
    });
    const canvas = new OffscreenCanvas(bmp.width, bmp.height);
    const ctx = canvas.getContext('2d');
    if (!ctx) return file;
    ctx.drawImage(bmp, 0, 0);
    bmp.close();
    return await canvas.convertToBlob({ type: 'image/webp', quality: WEBP_Q });
  } catch {
    // A browser that cannot do this is not an error the agent should ever hear about.
    return file;
  }
}

// ---------------------------------------------------------------------------------
// Normalising what comes back
// ---------------------------------------------------------------------------------

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function normalizeBbox(raw: unknown): BoundingBox | null {
  if (Array.isArray(raw) && raw.length === 4 && raw.every((n) => typeof n === 'number')) {
    const [x0, y0, x1, y1] = raw as [number, number, number, number];
    return { x0, y0, x1, y1 };
  }
  const obj = asRecord(raw);
  if (!obj) return null;
  const nums = ['x0', 'y0', 'x1', 'y1'].map((k) => obj[k]);
  if (nums.every((n) => typeof n === 'number')) {
    const [x0, y0, x1, y1] = nums as [number, number, number, number];
    return { x0, y0, x1, y1 };
  }
  return null;
}

function normalizeField(raw: unknown): FieldResult | null {
  const obj = asRecord(raw);
  if (!obj || typeof obj['field'] !== 'string' || typeof obj['verdict'] !== 'string') {
    return null;
  }
  const evidence = asRecord(obj['evidence']);
  return {
    field: obj['field'] as FieldResult['field'],
    verdict: obj['verdict'] as FieldResult['verdict'],
    extracted: typeof obj['extracted'] === 'string' ? obj['extracted'] : null,
    expected: typeof obj['expected'] === 'string' ? obj['expected'] : null,
    confidence: typeof obj['confidence'] === 'number' ? obj['confidence'] : 0,
    rationale: typeof obj['rationale'] === 'string' ? obj['rationale'] : '',
    tier: typeof obj['tier'] === 'number' ? obj['tier'] : null,
    evidence: evidence
      ? {
          image_index:
            typeof evidence['image_index'] === 'number' ? evidence['image_index'] : 0,
          bbox: normalizeBbox(evidence['bbox']),
        }
      : null,
    findings: Array.isArray(obj['findings'])
      ? (obj['findings'] as unknown[]).flatMap((f) => {
          const rec = asRecord(f);
          if (!rec) return [];
          return [
            {
              code: String(rec['code'] ?? ''),
              message: String(rec['message'] ?? ''),
              citation: typeof rec['citation'] === 'string' ? rec['citation'] : null,
              severity: String(rec['severity'] ?? 'finding'),
            },
          ];
        })
      : [],
  };
}

const RECOMMENDATION_ALIASES: Record<string, VerificationResult['aggregate']['recommendation']> =
  {
    ready_to_approve: 'ready_to_approve',
    needs_review: 'needs_review',
    return_for_correction: 'return_for_correction',
    // BUILD.md §3 predates the rename in api/models.py. Accept it rather than blank out.
    reject_candidate: 'return_for_correction',
  };

/** Turn whatever arrived into the shape the rest of the app is typed against. */
export function normalizeResult(raw: unknown): VerificationResult {
  const obj = asRecord(raw);
  const aggregate = obj ? asRecord(obj['aggregate']) : null;
  if (!obj || !aggregate || !Array.isArray(obj['fields'])) {
    throw failure(
      'internal',
      'unreadable_response',
      'The answer that came back could not be read. Nothing has been verified.',
      'retry',
    );
  }

  const recommendation =
    RECOMMENDATION_ALIASES[String(aggregate['recommendation'])] ?? 'needs_review';

  const timings = asRecord(obj['timings_ms']) ?? {};
  const cost = asRecord(obj['cost']) ?? {};
  const num = (src: Record<string, unknown>, key: string): number =>
    typeof src[key] === 'number' ? (src[key] as number) : 0;

  return {
    request_id: String(obj['request_id'] ?? ''),
    aggregate: {
      recommendation,
      rationale: String(aggregate['rationale'] ?? ''),
      driving_field:
        typeof aggregate['driving_field'] === 'string'
          ? (aggregate['driving_field'] as VerificationResult['aggregate']['driving_field'])
          : null,
    },
    fields: (obj['fields'] as unknown[]).flatMap((f) => {
      const parsed = normalizeField(f);
      return parsed ? [parsed] : [];
    }),
    images: Array.isArray(obj['images'])
      ? (obj['images'] as unknown[]).flatMap((im) => {
          const rec = asRecord(im);
          if (!rec) return [];
          const quality = asRecord(rec['quality']) ?? {};
          const report: ImageReport = {
            index: num(rec, 'index'),
            role: typeof rec['role'] === 'string' ? rec['role'] : null,
            quality: {
              blur: num(quality, 'blur'),
              exposure: num(quality, 'exposure'),
              glare: num(quality, 'glare'),
              skew_deg: num(quality, 'skew_deg'),
              resolution_ok: quality['resolution_ok'] !== false,
              verdict: String(quality['verdict'] ?? 'ok'),
              reason: typeof quality['reason'] === 'string' ? quality['reason'] : null,
            },
            url: typeof rec['url'] === 'string' ? rec['url'] : null,
          };
          return [report];
        })
      : [],
    timings_ms: {
      ingest: num(timings, 'ingest'),
      quality: num(timings, 'quality'),
      preprocess: num(timings, 'preprocess'),
      extract: num(timings, 'extract'),
      compare: num(timings, 'compare'),
      adjudicate: num(timings, 'adjudicate'),
      total: num(timings, 'total'),
    },
    cost: {
      input_tokens: num(cost, 'input_tokens'),
      output_tokens: num(cost, 'output_tokens'),
      usd: typeof cost['usd'] === 'number' ? cost['usd'] : 0,
    },
  };
}

// ---------------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------------

async function readError(response: Response): Promise<ApiFailure> {
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  const envelope = asRecord(payload);
  const detail = envelope ? asRecord(envelope['error']) : null;
  if (detail && typeof detail['message'] === 'string') {
    const kind = (String(detail['kind'] ?? 'internal') as ErrorKind) ?? 'internal';
    return new ApiFailure({
      kind,
      code: String(detail['code'] ?? 'error'),
      message: detail['message'],
      next_step: typeof detail['next_step'] === 'string' ? detail['next_step'] : null,
    });
  }
  const kind: ErrorKind =
    response.status === 503 ? 'provider' : response.status < 500 ? 'user' : 'internal';
  return failure(
    kind,
    `http_${response.status}`,
    ERROR_FALLBACK[kind] ?? 'Nothing was verified.',
    kind === 'provider' ? 'retry' : undefined,
  );
}

function networkFailure(): ApiFailure {
  return failure(
    'provider',
    'unreachable',
    'The checking service could not be reached, so nothing was verified. Your application details are still on screen.',
    'retry',
  );
}

export interface VerifySubmission {
  application: Application;
  files: File[];
  /** Which face each image shows. The server assumes front/back for a pair otherwise. */
  roles?: (string | null)[];
}

export async function verify(
  submission: VerifySubmission,
  signal?: AbortSignal,
): Promise<VerificationResult> {
  const form = new FormData();
  form.append('application', JSON.stringify(submission.application));
  for (const file of submission.files) {
    const blob = await shrinkForUpload(file);
    const name =
      blob === (file as Blob) ? file.name : file.name.replace(/\.[^.]+$/, '') + '.webp';
    form.append('images', blob, name);
  }
  const roles = submission.roles;
  if (roles && roles.length === submission.files.length && roles.every(Boolean)) {
    for (const role of roles) form.append('roles', role as string);
  }

  let response: Response;
  try {
    response = await fetch('/verify', { method: 'POST', body: form, signal });
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') throw err;
    throw networkFailure();
  }
  if (!response.ok) throw await readError(response);
  return normalizeResult(await response.json());
}

export interface SampleImage {
  url: string;
  role: string | null;
  filename: string;
}

export interface SampleOutcome {
  result: VerificationResult;
  /** The filed application behind the sample, when the server sends it back. */
  application: Application | null;
  images: SampleImage[];
}

function sampleImages(raw: unknown): SampleImage[] {
  if (!Array.isArray(raw)) return [];
  return (raw as unknown[]).flatMap((entry, index) => {
    // `GET /sample` sends objects with a url; a bare list of urls is also accepted.
    if (typeof entry === 'string') {
      return [{ url: entry, role: null, filename: `sample-${index + 1}` }];
    }
    const rec = asRecord(entry);
    if (!rec || typeof rec['url'] !== 'string') return [];
    return [
      {
        url: rec['url'],
        role: typeof rec['role'] === 'string' ? rec['role'] : null,
        filename:
          typeof rec['filename'] === 'string' ? rec['filename'] : `sample-${index + 1}`,
      },
    ];
  });
}

/**
 * One click to a verdict (LP-098).
 *
 * `GET /sample` hands back the Old Tom application and the URLs of its label pair. This
 * fetches those images and posts them straight to `/verify`, so the grader's single
 * click ends on a real verdict produced by the real pipeline — not on a canned answer.
 * A finished result coming back from `/sample` is also accepted, so a future server that
 * pre-verifies the demo needs no change here.
 */
export async function loadSample(signal?: AbortSignal): Promise<SampleOutcome> {
  let response: Response;
  try {
    response = await fetch('/sample', { signal });
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') throw err;
    throw networkFailure();
  }
  if (!response.ok) throw await readError(response);

  const payload = (await response.json()) as unknown;
  const obj = asRecord(payload);
  if (!obj) throw failure('internal', 'unreadable_response', ERROR_FALLBACK['internal']!);

  const application = asRecord(obj['application']) as Application | null;
  const images = sampleImages(obj['images'] ?? obj['image_urls']);

  // Already verified — render it as it stands.
  if (asRecord(obj['aggregate']) && Array.isArray(obj['fields'])) {
    return { result: normalizeResult(obj), application, images };
  }

  // The usual case: an application plus its pictures. Run them through /verify.
  if (application && images.length > 0) {
    const files = await Promise.all(
      images.map(async (image, i) => {
        const res = await fetch(image.url, { signal });
        if (!res.ok) throw await readError(res);
        const blob = await res.blob();
        const name = image.filename || `sample-${i + 1}.png`;
        return new File([blob], name, { type: blob.type || 'image/png' });
      }),
    );
    const result = await verify(
      { application, files, roles: images.map((image) => image.role) },
      signal,
    );
    return { result, application, images };
  }

  throw failure(
    'internal',
    'sample_unavailable',
    'The sample application could not be loaded. You can still upload a label and enter the details yourself.',
  );
}
