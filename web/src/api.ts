/**
 * The only place this app talks to the server.
 *
 * Two jobs beyond fetching:
 *
 *   1. **Shrink before sending.** The build spec pinned the client-side encode: resize the
 *      long edge to 2576px with `createImageBitmap`, encode WebP off the main thread.
 *      Never below 2576 — that is the high-res tier the small warning text needs.
 *   2. **Be forgiving at the boundary, strict inside.** The server is being built in
 *      parallel. Anything the UI can safely tolerate (a bbox as an array instead of an
 *      object, the older `reject_candidate` spelling) is normalised here so the rest of
 *      the app sees exactly the shape in `types.ts`. Nothing is invented: a value that
 *      is not there stays absent, and an absent bbox stays absent, because a guessed
 *      region is a false trust signal (pinned build decision).
 */

import type {
  ApiError,
  Application,
  BatchAccepted,
  BatchItem,
  BatchStatus,
  BoundingBox,
  Cost,
  ErrorKind,
  FieldResult,
  ImageReport,
  ItemState,
  JobCounts,
  JobState,
  RowError,
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
    // the build spec predates the rename in api/models.py. Accept it rather than blank out.
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
  /**
   * For a stage that reports null when it did not run. Coercing that to 0 through `num`
   * would undo the whole reason the server sends null: `adjudicate: 0` reads as
   * "adjudication ran and took no time", which is the opposite of what happened.
   */
  const nullableNum = (src: Record<string, unknown>, key: string): number | null =>
    typeof src[key] === 'number' ? (src[key] as number) : null;

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
      adjudicate: nullableNum(timings, 'adjudicate'),
      total: num(timings, 'total'),
    },
    cost: {
      input_tokens: num(cost, 'input_tokens'),
      output_tokens: num(cost, 'output_tokens'),
      cache_read_tokens: num(cost, 'cache_read_tokens'),
      cache_creation_tokens: num(cost, 'cache_creation_tokens'),
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
  /** A reading already taken for exactly these files. Ignored by the server if it is not. */
  preparedToken?: string;
  /** Which face each image shows. The server assumes front/back for a pair otherwise. */
  roles?: (string | null)[];
}

/** A reading the server took while the agent was still typing. */
export interface PreparedReading {
  token: string;
  /** What the reading cost in wall clock, so the result can report the work not the wait. */
  readMs: number;
}

/**
 * Ask the server to read the label now, before the form is finished.
 *
 * Extraction takes only the commodity from the application, so it can start the moment
 * the pictures exist. By the time six fields are typed the reading is usually done and
 * pressing the button costs the comparison alone.
 *
 * Returns null for every unhappy path — no reading, provider down, endpoint missing,
 * offline. The caller submits normally and loses nothing but the head start, so there is
 * no error here worth showing to someone who has not asked for anything yet.
 */
export async function prepareReading(
  files: File[],
  commodity: string,
  signal?: AbortSignal,
): Promise<PreparedReading | null> {
  if (files.length === 0) return null;
  const form = new FormData();
  form.append('commodity', commodity);
  for (const file of files) {
    const blob = await shrinkForUpload(file);
    const name =
      blob === (file as Blob) ? file.name : file.name.replace(/\.[^.]+$/, '') + '.webp';
    form.append('images', blob, name);
  }
  try {
    const response = await fetch('/prepare', { method: 'POST', body: form, signal });
    if (!response.ok) return null;
    const body = asRecord(await response.json());
    if (!body || body['prepared'] !== true || typeof body['token'] !== 'string') return null;
    return {
      token: body['token'],
      readMs: typeof body['read_ms'] === 'number' ? body['read_ms'] : 0,
    };
  } catch {
    return null;
  }
}

export async function verify(
  submission: VerifySubmission,
  signal?: AbortSignal,
): Promise<VerificationResult> {
  const form = new FormData();
  form.append('application', JSON.stringify(submission.application));
  if (submission.preparedToken) form.append('prepared_token', submission.preparedToken);
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

/** One demo the server offers, described by what a reviewer will see. */
export interface SampleCase {
  slug: string;
  title: string;
  summary: string;
}

export interface SampleOutcome {
  result: VerificationResult;
  /** The filed application behind the sample, when the server sends it back. */
  application: Application | null;
  images: SampleImage[];
  /** Every sample on offer, so the page can show the rest after running one. */
  cases: SampleCase[];
  /** Which one just ran. */
  slug: string;
}

function sampleCases(raw: unknown): SampleCase[] {
  if (!Array.isArray(raw)) return [];
  return (raw as unknown[]).flatMap((entry) => {
    const rec = asRecord(entry);
    if (!rec || typeof rec['slug'] !== 'string') return [];
    return [
      {
        slug: rec['slug'],
        title: String(rec['title'] ?? rec['slug']),
        summary: String(rec['summary'] ?? ''),
      },
    ];
  });
}

/**
 * The samples on offer, without running one.
 *
 * Asked for on mount so the setup screen can show what is available before a reviewer
 * commits to a click. A failure here is silent by design: the buttons simply do not
 * appear, and manual entry — which is the rest of the screen — still works.
 */
export async function listSamples(signal?: AbortSignal): Promise<SampleCase[]> {
  try {
    const response = await fetch('/sample', { signal });
    if (!response.ok) return [];
    const obj = asRecord(await response.json());
    return obj ? sampleCases(obj['cases']) : [];
  } catch {
    return [];
  }
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
export async function loadSample(
  slug?: string,
  signal?: AbortSignal,
): Promise<SampleOutcome> {
  let response: Response;
  try {
    const query = slug ? `?case=${encodeURIComponent(slug)}` : '';
    response = await fetch(`/sample${query}`, { signal });
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
  const cases = sampleCases(obj['cases']);
  const ran = typeof obj['slug'] === 'string' ? obj['slug'] : (slug ?? '');

  // Already verified — render it as it stands.
  if (asRecord(obj['aggregate']) && Array.isArray(obj['fields'])) {
    return { result: normalizeResult(obj), application, images, cases, slug: ran };
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
    return { result, application, images, cases, slug: ran };
  }

  throw failure(
    'internal',
    'sample_unavailable',
    'The sample application could not be loaded. You can still upload a label and enter the details yourself.',
  );
}

// ---------------------------------------------------------------------------------
// Batch (BATCH-1..10)
// ---------------------------------------------------------------------------------

/**
 * Where the manifest template lives. A plain link rather than a fetch: the browser's own
 * download is more reliable than anything we would build, and an agent who wants to see
 * the columns before committing to a batch should be able to just open it (UX-1).
 */
export const MANIFEST_TEMPLATE_URL = '/batch/manifest-template.csv';

/** The CSV export for a finished job (BATCH-7). Also a plain link, for the same reason. */
export function batchExportUrl(jobId: string): string {
  return `/batch/${encodeURIComponent(jobId)}/export.csv`;
}

/**
 * Images are NOT shrunk on the way into a batch, and that is deliberate.
 *
 * `verify()` re-encodes each file because it is one or two images and the round trip is
 * the thing an agent is waiting on. A batch is up to 300 applications and their labels,
 * frequently arriving as one zip the agent assembled elsewhere. Re-encoding every entry
 * would mean decoding a few hundred images on the main thread before the first byte is
 * uploaded — a progress bar that sits at zero for a minute while the tab freezes, to save
 * bandwidth on a request nobody is watching in real time.
 *
 * The server bounds what it will accept on the wire either way (`api.main._WireLimit`),
 * so the protection this would add is already there.
 */
export async function createBatch(
  manifest: File,
  images: File[],
  signal?: AbortSignal,
): Promise<BatchAccepted> {
  const form = new FormData();
  form.append('manifest', manifest, manifest.name);
  for (const file of images) form.append('files', file, file.name);

  let response: Response;
  try {
    response = await fetch('/batch', { method: 'POST', body: form, signal });
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') throw err;
    throw networkFailure();
  }
  if (!response.ok) throw await readError(response);

  const obj = asRecord(await response.json());
  if (!obj) throw failure('internal', 'unreadable_response', ERROR_FALLBACK['internal']!);
  return {
    job_id: String(obj['job_id'] ?? ''),
    accepted: typeof obj['accepted'] === 'number' ? obj['accepted'] : 0,
    row_errors: normalizeRowErrors(obj['row_errors']),
    unmatched_files: normalizeStrings(obj['unmatched_files']),
    message: typeof obj['message'] === 'string' ? obj['message'] : '',
  };
}

function normalizeStrings(raw: unknown): string[] {
  return Array.isArray(raw) ? raw.filter((v): v is string => typeof v === 'string') : [];
}

function normalizeRowErrors(raw: unknown): RowError[] {
  if (!Array.isArray(raw)) return [];
  return (raw as unknown[]).flatMap((entry) => {
    const rec = asRecord(entry);
    if (!rec) return [];
    return [
      {
        row: typeof rec['row'] === 'number' ? rec['row'] : 0,
        column: typeof rec['column'] === 'string' ? rec['column'] : null,
        message: String(rec['message'] ?? ''),
      },
    ];
  });
}

function normalizeCounts(raw: unknown): JobCounts {
  const rec = asRecord(raw) ?? {};
  const n = (key: string): number => (typeof rec[key] === 'number' ? (rec[key] as number) : 0);
  return {
    total: n('total'),
    queued: n('queued'),
    processing: n('processing'),
    done: n('done'),
    failed: n('failed'),
  };
}

function normalizeCost(raw: unknown): Cost {
  const rec = asRecord(raw) ?? {};
  const n = (key: string): number => (typeof rec[key] === 'number' ? (rec[key] as number) : 0);
  return {
    input_tokens: n('input_tokens'),
    output_tokens: n('output_tokens'),
    cache_read_tokens: n('cache_read_tokens'),
    cache_creation_tokens: n('cache_creation_tokens'),
    usd: n('usd'),
  };
}

const ITEM_STATES: readonly ItemState[] = ['queued', 'processing', 'done', 'failed'];

function normalizeItem(raw: unknown): BatchItem | null {
  const rec = asRecord(raw);
  if (!rec || typeof rec['item_id'] !== 'string') return null;

  const state = ITEM_STATES.includes(rec['state'] as ItemState)
    ? (rec['state'] as ItemState)
    : 'queued';
  const failure_ = asRecord(rec['failure']);

  return {
    item_id: rec['item_id'],
    job_id: typeof rec['job_id'] === 'string' ? rec['job_id'] : '',
    row: typeof rec['row'] === 'number' ? rec['row'] : 0,
    state,
    attempts: typeof rec['attempts'] === 'number' ? rec['attempts'] : 0,
    application: (asRecord(rec['application']) ?? {}) as unknown as Application,
    images: normalizeStrings(rec['images']),
    // A result is only rendered when the server actually sent one. An item that has not
    // finished has no verdicts, and inventing an empty set would put seven blank rows on
    // screen that read as "nothing wrong here".
    result: asRecord(rec['result']) ? normalizeResult(rec['result']) : null,
    failure: failure_
      ? {
          code: String(failure_['code'] ?? ''),
          message: String(failure_['message'] ?? ''),
          next_step: String(failure_['next_step'] ?? 'retry'),
          attempts: typeof failure_['attempts'] === 'number' ? failure_['attempts'] : 0,
        }
      : null,
    created_at: typeof rec['created_at'] === 'number' ? rec['created_at'] : 0,
    started_at: typeof rec['started_at'] === 'number' ? rec['started_at'] : null,
    finished_at: typeof rec['finished_at'] === 'number' ? rec['finished_at'] : null,
  };
}

function normalizeStatus(raw: unknown): BatchStatus {
  const obj = asRecord(raw);
  if (!obj) throw failure('internal', 'unreadable_response', ERROR_FALLBACK['internal']!);

  const summary = asRecord(obj['summary']) ?? {};
  const counts = normalizeCounts(obj['counts']);

  return {
    job_id: String(obj['job_id'] ?? ''),
    state: (['queued', 'processing', 'done'] as JobState[]).includes(obj['state'] as JobState)
      ? (obj['state'] as JobState)
      : 'processing',
    counts,
    eta_seconds: typeof obj['eta_seconds'] === 'number' ? obj['eta_seconds'] : null,
    summary: {
      by_recommendation: (asRecord(summary['by_recommendation']) ?? {}) as Record<string, number>,
      by_verdict: (asRecord(summary['by_verdict']) ?? {}) as Record<string, number>,
      worst_first: normalizeStrings(summary['worst_first']),
      headline: typeof summary['headline'] === 'string' ? summary['headline'] : '',
    },
    items: Array.isArray(obj['items'])
      ? (obj['items'] as unknown[]).flatMap((i) => normalizeItem(i) ?? [])
      : [],
    cost: normalizeCost(obj['cost']),
    row_errors: normalizeRowErrors(obj['row_errors']),
    unmatched_files: normalizeStrings(obj['unmatched_files']),
    expires_at: typeof obj['expires_at'] === 'number' ? obj['expires_at'] : 0,
    message: typeof obj['message'] === 'string' ? obj['message'] : '',
  };
}

/**
 * Poll one job. `include_pending` asks for the queued and in-flight rows too, so the
 * table can show every application from the first tick instead of growing from nothing —
 * an agent watching 300 rows needs to see the 300, not wonder where they went.
 */
export async function batchStatus(
  jobId: string,
  options: { includePending?: boolean; limit?: number } = {},
  signal?: AbortSignal,
): Promise<BatchStatus> {
  const params = new URLSearchParams();
  if (options.includePending) params.set('include_pending', 'true');
  if (options.limit != null) params.set('limit', String(options.limit));
  const query = params.toString();
  const url = `/batch/${encodeURIComponent(jobId)}${query ? `?${query}` : ''}`;

  let response: Response;
  try {
    response = await fetch(url, { signal });
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') throw err;
    throw networkFailure();
  }
  if (!response.ok) throw await readError(response);
  return normalizeStatus(await response.json());
}

/** Requeue the failed items and nothing else (BATCH-8). Returns the refreshed status. */
export async function retryBatch(jobId: string, signal?: AbortSignal): Promise<BatchStatus> {
  let response: Response;
  try {
    response = await fetch(`/batch/${encodeURIComponent(jobId)}/retry`, {
      method: 'POST',
      signal,
    });
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') throw err;
    throw networkFailure();
  }
  if (!response.ok) throw await readError(response);
  return normalizeStatus(await response.json());
}
