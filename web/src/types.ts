/**
 * The wire contract, mirrored from `api/models.py`.
 *
 * These types are a copy of the server's pydantic models, not an interpretation of them.
 * Nothing is added here that the server does not send. Where the build spec and
 * `api/models.py` disagree on a spelling, `models.py` is what actually serializes, so
 * that is what these types name — with the older spelling accepted at the parse boundary
 * in `api.ts` so a stale server cannot blank the screen.
 */

export type Commodity = 'spirits' | 'wine' | 'malt';

/** Exactly six. Adding a seventh is a product decision (MATCH-1). */
export type Verdict =
  | 'match'
  | 'acceptable_variation'
  | 'mismatch'
  | 'missing'
  | 'unreadable'
  | 'not_applicable';

/** The app recommends; the agent decides (HITL-1). */
export type Recommendation =
  | 'ready_to_approve'
  | 'needs_review'
  | 'return_for_correction';

export type FieldName =
  | 'brand_name'
  | 'class_type'
  | 'alcohol_content'
  | 'net_contents'
  | 'producer'
  | 'country_of_origin'
  | 'government_warning';

export interface Application {
  commodity: Commodity;
  brand_name: string;
  class_type: string;
  alcohol_content: number | null;
  net_contents: string;
  producer_name: string;
  producer_address: string;
  country_of_origin: string | null;
  is_import: boolean;
}

/** Normalized 0..1 against the PREPROCESSED image. */
export interface BoundingBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface Evidence {
  image_index: number;
  /** Absent when the extractor gave no region. Absent means draw nothing. */
  bbox: BoundingBox | null;
}

export interface Finding {
  code: string;
  message: string;
  citation: string | null;
  severity: string;
}

export interface FieldResult {
  field: FieldName;
  verdict: Verdict;
  extracted: string | null;
  expected: string | null;
  confidence: number;
  rationale: string;
  tier: number | null;
  evidence: Evidence | null;
  findings: Finding[];
}

export interface Aggregate {
  recommendation: Recommendation;
  rationale: string;
  driving_field: FieldName | null;
}

export interface ImageQuality {
  blur: number;
  exposure: number;
  glare: number;
  skew_deg: number;
  resolution_ok: boolean;
  /** "ok" | "degraded" | "hopeless" */
  verdict: string;
  reason: string | null;
}

export interface ImageReport {
  index: number;
  role: string | null;
  quality: ImageQuality;
  /**
   * URL of the PREPROCESSED image the evidence boxes were measured against.
   *
   * Optional because the server does not send it today (see the contract gap noted in
   * `EvidenceOverlay.tsx`). When it is absent the UI falls back to the local upload.
   */
  url?: string | null;
}

export interface Timings {
  ingest: number;
  quality: number;
  preprocess: number;
  extract: number;
  compare: number;
  /**
   * Tier-3 text adjudication. **Null, not zero, when the stage did not run** — and it
   * does not run in this build at all. The two are different facts: `0` reads as
   * "instant", so a reader taking `adjudicate: 0` at face value concludes adjudication
   * ran and cost nothing. See `api/timing.UNIMPLEMENTED_STAGES`.
   */
  adjudicate: number | null;
  total: number;
}

export interface Cost {
  input_tokens: number;
  output_tokens: number;
  /**
   * The two cache counters are carried separately because they are PRICED separately: a
   * cached read costs a tenth of an input token, writing a cache entry costs 1.25x one,
   * and `input_tokens` excludes both. Dropping either does not make the total
   * conservative — it makes those tokens free.
   */
  cache_read_tokens: number;
  cache_creation_tokens: number;
  usd: number;
}

export interface VerificationResult {
  request_id: string;
  aggregate: Aggregate;
  fields: FieldResult[];
  images: ImageReport[];
  timings_ms: Timings;
  cost: Cost;
}

/** Error envelope. `kind` and `next_step` drive what the UI offers to do next (UX-6). */
export type ErrorKind = 'user' | 'image' | 'provider' | 'internal';

export interface ApiError {
  kind: ErrorKind;
  code: string;
  message: string;
  next_step?: string | null;
}

/** What the agent did with a row. Session only — nothing is filed anywhere (SCOPE-3). */
export type AgentDecision = 'confirmed' | 'overridden';

/**
 * A value the agent read off the bottle themselves, because the picture could not be
 * read (UX-6, HITL-2).
 *
 * Deliberately NOT merged into `FieldResult.extracted`. `extracted` means "this is what
 * the label image says, as read by the model", and an agent's typing is a different kind
 * of fact with a different basis. Writing it into the same slot would make the report
 * claim the image was verified when it never was — the one thing this whole application
 * is built not to do — and nothing downstream could tell the two apart afterwards.
 *
 * So it rides alongside, and the row keeps its Unreadable verdict.
 */
export interface AgentEntry {
  value: string;
  /** True when what the agent typed matches what the application says. Advisory only. */
  agrees: boolean;
}

// ---------------------------------------------------------------------------------
// Batch (BATCH-1..10) — mirrored from `api/batch/models.py`
// ---------------------------------------------------------------------------------

export type ItemState = 'queued' | 'processing' | 'done' | 'failed';

/**
 * There is deliberately no `failed` job state on the server. A job whose items all
 * failed is a *finished* job with N failed items, and the screen says so rather than
 * collapsing it to one word that hides which ones (BATCH-6).
 */
export type JobState = 'queued' | 'processing' | 'done';

/** A manifest row the server could not use. Carries the row number so it is fixable. */
export interface RowError {
  row: number;
  column: string | null;
  message: string;
}

export interface ItemFailure {
  code: string;
  message: string;
  next_step: string;
  attempts: number;
}

export interface BatchItem {
  item_id: string;
  job_id: string;
  row: number;
  state: ItemState;
  attempts: number;
  application: Application;
  images: string[];
  result: VerificationResult | null;
  failure: ItemFailure | null;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
}

export interface JobCounts {
  total: number;
  queued: number;
  processing: number;
  done: number;
  failed: number;
}

export interface BatchSummary {
  by_recommendation: Record<string, number>;
  by_verdict: Record<string, number>;
  /** Item IDs in the order an agent should work them. Computed server-side (UX-10). */
  worst_first: string[];
  headline: string;
}

export interface BatchStatus {
  job_id: string;
  state: JobState;
  counts: JobCounts;
  eta_seconds: number | null;
  summary: BatchSummary;
  items: BatchItem[];
  cost: Cost;
  row_errors: RowError[];
  unmatched_files: string[];
  expires_at: number;
  message: string;
}

/**
 * `POST /batch`. Row errors ride along WITH the job id rather than replacing it: three
 * bad rows out of 300 is not a rejected upload, and making an agent fix a typo before any
 * work starts is the batch equivalent of doing them one at a time (TC-20).
 */
export interface BatchAccepted {
  job_id: string;
  accepted: number;
  row_errors: RowError[];
  unmatched_files: string[];
  message: string;
}
