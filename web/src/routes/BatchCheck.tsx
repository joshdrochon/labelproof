/**
 * Batch check (BATCH-1..10, LP-167..LP-182).
 *
 * Three screens in one route, because they are three moments in one task and an agent
 * should never wonder which page they are on:
 *
 *   1. **Upload** — a manifest and the images it names.
 *   2. **Running** — counts, a bar, an ETA, and results appearing as they finish.
 *   3. **Finished** — the same table, plus retry and export.
 *
 * Two decisions worth stating, because both were the other way at first:
 *
 * **Results are shown while the job runs (BATCH-5).** The table is not gated on the job
 * finishing. A 300-application batch takes minutes and the agent can start triaging the
 * first rejections inside the first few seconds. Waiting for the last row before showing
 * the first is a spinner with extra steps.
 *
 * **The order is the server's, not ours.** `summary.worst_first` is computed in
 * `api/batch/models.py` from the same ladder the single-label view uses. Re-sorting here
 * would let the screen and the recommendation drift apart, which is the bug that makes an
 * agent stop trusting the ordering entirely. Filters *hide* rows; they never reorder them.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  ApiFailure,
  MANIFEST_TEMPLATE_URL,
  batchExportUrl,
  batchStatus,
  createBatch,
  createSampleBatch,
  retryBatch,
} from '../api';
import type { BatchAccepted } from '../types';
import { RECOMMENDATIONS, VERDICTS, fieldLabel } from '../copy';
import { VerdictGlyph } from '../components/VerdictCard';
import type {
  AgentDecision,
  ApiError,
  BatchItem,
  BatchStatus,
  FieldName,
  ItemState,
  Recommendation,
} from '../types';
import ItemDetail from '../components/ItemDetail';

/** How often to ask the server how it is going. */
const POLL_MS = 1500;

/**
 * Rows requested per poll. The server caps this itself (`MAX_ITEM_LIMIT`); asking for
 * more than a screen's worth is still right, because the filters work on what we have and
 * an agent scrolling should not hit a wall that looks like the batch ending early.
 */
const ITEM_LIMIT = 500;

type Filter = 'all' | Recommendation | 'failed';

const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'return_for_correction', label: 'Return for correction' },
  { id: 'failed', label: 'Could not check' },
  { id: 'needs_review', label: 'Needs review' },
  { id: 'ready_to_approve', label: 'Ready to approve' },
];

function bucketOf(item: BatchItem): Filter | 'pending' {
  if (item.state === 'failed') return 'failed';
  if (item.result === null) return 'pending';
  return item.result.aggregate.recommendation;
}

function countIn(items: BatchItem[], filter: Filter): number {
  if (filter === 'all') return items.length;
  return items.filter((item) => bucketOf(item) === filter).length;
}

/** Plain words. "ETA 00:03:20" is a stopwatch; an agent wants to know whether to wait. */
export function formatEta(seconds: number | null): string {
  if (seconds === null) return 'Working out how long this will take…';
  if (seconds <= 0) return 'Almost done.';
  if (seconds < 60) return `About ${Math.max(5, Math.round(seconds / 5) * 5)} seconds left.`;
  const minutes = Math.round(seconds / 60);
  return `About ${minutes} minute${minutes === 1 ? '' : 's'} left.`;
}

export function percentDone(done: number, failed: number, total: number): number {
  if (total <= 0) return 0;
  return Math.min(100, Math.round(((done + failed) / total) * 100));
}

export default function BatchCheck() {
  const [manifest, setManifest] = useState<File | null>(null);
  const [images, setImages] = useState<File[]>([]);
  const [status, setStatus] = useState<BatchStatus | null>(null);
  const [problem, setProblem] = useState<ApiError | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [filter, setFilter] = useState<Filter>('all');
  const [openItem, setOpenItem] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  // Whether the job on screen is the demonstration one. It changes exactly one sentence —
  // the explanation beside the bad manifest row — and nothing about how the job is run.
  const [isSample, setIsSample] = useState(false);

  /**
   * Decisions this screen has written, by item, laid over whatever the poll last said.
   *
   * Two reasons it lives here and not in the dialog. **A finished job stops polling** —
   * see the effect below — so after the last tick nothing ever refreshes `item.decisions`
   * again, and a ruling made in a dialog that then closed had nowhere to survive. That is
   * the state almost all triage happens in, and it made the feature look like it worked
   * only because a running job's polls were papering over it.
   *
   * And while a job IS running, this overlay is what makes the ordering safe: a status
   * request that left before a write can land after it, and without an overlay the poll's
   * stale copy would win and flip a button back.
   */
  const [decisions, setDecisions] = useState<
    Record<string, Partial<Record<FieldName, AgentDecision>>>
  >({});

  const abort = useRef<AbortController | null>(null);
  const jobId = status?.job_id ?? null;
  const running = status !== null && status.state !== 'done';

  // --- polling ----------------------------------------------------------------------
  //
  // One timeout chained off the previous response rather than setInterval: a slow reply
  // must not stack requests behind it, and a batch under load is exactly when replies get
  // slow. Stops when the job is done — a finished job cannot change, so continuing to
  // poll it would be a request every 1.5s forever on an idle tab.
  useEffect(() => {
    if (!jobId || !running) return undefined;

    let cancelled = false;
    let timer = 0;

    const poll = async () => {
      const controller = new AbortController();
      abort.current = controller;
      try {
        const next = await batchStatus(
          jobId,
          { includePending: true, limit: ITEM_LIMIT },
          controller.signal,
        );
        if (cancelled) return;
        setStatus(next);
        if (next.state !== 'done') timer = window.setTimeout(poll, POLL_MS);
      } catch (err) {
        if (cancelled || (err as Error)?.name === 'AbortError') return;
        // A failed poll is not a failed batch. The work continues on the server; say so
        // and keep trying, rather than throwing away a table the agent is reading.
        setProblem(err instanceof ApiFailure ? err.detail : null);
        timer = window.setTimeout(poll, POLL_MS * 2);
      }
    };

    // Immediately, not after POLL_MS. The agent has just pressed a button; making them
    // watch an empty table for a second and a half to learn the job exists is the kind of
    // dead air that reads as "it did not work".
    timer = window.setTimeout(poll, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      abort.current?.abort();
    };
  }, [jobId, running]);

  /**
   * Straight into the running screen with the counts we already know, so the agent sees
   * the batch exists before the first poll comes back.
   *
   * Shared by the upload and the sample on purpose. A demonstration that took a different
   * path through this screen would demonstrate the path, not the product — the sample has
   * to queue, poll, order and export exactly as an uploaded batch does, or it is worth
   * nothing to the person deciding whether to trust it.
   */
  const beginJob = useCallback((accepted: BatchAccepted, sample: boolean) => {
    setIsSample(sample);
    setStatus({
      job_id: accepted.job_id,
      state: 'queued',
      counts: {
        total: accepted.accepted,
        queued: accepted.accepted,
        processing: 0,
        done: 0,
        failed: 0,
      },
      eta_seconds: null,
      summary: { by_recommendation: {}, by_verdict: {}, worst_first: [], headline: '' },
      items: [],
      cost: {
        input_tokens: 0,
        output_tokens: 0,
        cache_read_tokens: 0,
        cache_creation_tokens: 0,
        usd: 0,
      },
      row_errors: accepted.row_errors,
      unmatched_files: accepted.unmatched_files,
      expires_at: 0,
      message: accepted.message,
    });
  }, []);

  const submit = useCallback(async () => {
    if (!manifest || submitting) return;
    setSubmitting(true);
    setProblem(null);
    try {
      beginJob(await createBatch(manifest, images), false);
    } catch (err) {
      setProblem(err instanceof ApiFailure ? err.detail : null);
    } finally {
      setSubmitting(false);
    }
  }, [manifest, images, submitting, beginJob]);

  const startSample = useCallback(async () => {
    if (submitting) return;
    setSubmitting(true);
    setProblem(null);
    try {
      beginJob(await createSampleBatch(), true);
    } catch (err) {
      setProblem(err instanceof ApiFailure ? err.detail : null);
    } finally {
      setSubmitting(false);
    }
  }, [submitting, beginJob]);

  const onRetry = useCallback(async () => {
    if (!jobId || retrying) return;
    setRetrying(true);
    try {
      setStatus(await retryBatch(jobId));
    } catch (err) {
      setProblem(err instanceof ApiFailure ? err.detail : null);
    } finally {
      setRetrying(false);
    }
  }, [jobId, retrying]);

  const startOver = useCallback(() => {
    abort.current?.abort();
    setStatus(null);
    setManifest(null);
    setImages([]);
    setProblem(null);
    setFilter('all');
    setOpenItem(null);
    setIsSample(false);
    setDecisions({});
  }, []);

  // Memoised, both of them. `ItemDetail`'s focus effect depends on `onClose`, so a fresh
  // arrow each render re-runs it — and on a running job that is every 1.5s poll, each one
  // pulling focus back to the dialog heading while someone is mid-decision.
  const closeItem = useCallback(() => setOpenItem(null), []);

  const recordDecisions = useCallback(
    (itemId: string, updated: Partial<Record<FieldName, AgentDecision>>) => {
      setDecisions((current) => ({ ...current, [itemId]: updated }));
    },
    [],
  );

  if (status === null) {
    return (
      <BatchUpload
        manifest={manifest}
        images={images}
        onManifest={setManifest}
        onImages={setImages}
        onSubmit={submit}
        onSample={startSample}
        submitting={submitting}
        problem={problem}
      />
    );
  }

  return (
    <BatchResults
      status={status}
      isSample={isSample}
      decisions={decisions}
      onDecisions={recordDecisions}
      filter={filter}
      onFilter={setFilter}
      openItem={openItem}
      onOpenItem={setOpenItem}
      onCloseItem={closeItem}
      onRetry={onRetry}
      retrying={retrying}
      onStartOver={startOver}
      problem={problem}
    />
  );
}

// ---------------------------------------------------------------------------------
// 1. Upload
// ---------------------------------------------------------------------------------

function BatchUpload({
  manifest,
  images,
  onManifest,
  onImages,
  onSubmit,
  onSample,
  submitting,
  problem,
}: {
  manifest: File | null;
  images: File[];
  onManifest: (file: File | null) => void;
  onImages: (files: File[]) => void;
  onSubmit: () => void;
  onSample: () => void;
  submitting: boolean;
  problem: ApiError | null;
}) {
  return (
    <div className="batch">
      <header className="batch__intro">
        <h1 className="batch__title">Check a batch of applications</h1>
        <p className="batch__lede">
          Upload a spreadsheet of applications and the label images they name. Results
          appear as each one finishes — you can start reviewing before the batch ends.
        </p>
      </header>

      {problem ? <ProblemNote problem={problem} /> : null}

      <ol className="batch__steps">
        <li className="batch__step">
          <h2 className="batch__step-title">1. The spreadsheet</h2>
          <p className="batch__step-help">
            One row per application. The <code>images</code> column names the picture
            files for that row.{' '}
            <a className="link" href={MANIFEST_TEMPLATE_URL} download>
              Download a template
            </a>{' '}
            if you do not have one.
          </p>

          {/* Beside the template, because it answers the same question — "I do not have a
              spreadsheet yet" — and answers it harder. Verify Now's samples set the
              register: say what it loads and that there is nothing to fill in, then get
              out of the way. It runs the real queue on a real manifest; the only thing it
              saves the reviewer is assembling one. */}
          <div className="batch__sample">
            {/* Says six, because six get checked. The manifest has seven rows and one of
                them is deliberately broken — calling that "seven applications checked" is a
                small lie on the one screen whose entire argument is that it never claims
                more than it knows, and the reviewer meets the contradiction about four
                seconds later when the row-errors notice appears. */}
            <p className="batch__sample-note">
              Or try a sample batch. Six applications with their labels — different
              products, spirits and wine — checked right away, plus one deliberately broken
              row so you can see how that is reported. Nothing to upload.
            </p>
            <button
              type="button"
              className="btn btn--secondary"
              onClick={onSample}
              disabled={submitting}
            >
              Try a sample batch
            </button>
          </div>

          <label className="file-input">
            <span className="file-input__label">Spreadsheet (.csv)</span>
            <input
              type="file"
              accept=".csv,text/csv"
              disabled={submitting}
              onChange={(e) => onManifest(e.target.files?.[0] ?? null)}
            />
          </label>
          {manifest ? (
            <p className="batch__chosen">
              Chosen: <strong>{manifest.name}</strong>
            </p>
          ) : null}
        </li>

        <li className="batch__step">
          <h2 className="batch__step-title">2. The label images</h2>
          <p className="batch__step-help">
            Select every image the spreadsheet names, or one .zip containing them. Rows
            whose images are missing are reported by row number rather than failing the
            whole upload.
          </p>
          <label className="file-input">
            <span className="file-input__label">Images or a .zip</span>
            <input
              type="file"
              multiple
              accept="image/*,.zip,application/zip,.pdf"
              disabled={submitting}
              onChange={(e) => onImages(Array.from(e.target.files ?? []))}
            />
          </label>
          {images.length > 0 ? (
            <p className="batch__chosen">
              Chosen: <strong>{images.length}</strong> file
              {images.length === 1 ? '' : 's'}
            </p>
          ) : null}
        </li>
      </ol>

      <button
        type="button"
        className="btn btn--primary"
        onClick={onSubmit}
        disabled={!manifest || submitting}
      >
        {submitting ? 'Uploading…' : 'Start checking'}
      </button>
      {!manifest ? (
        <p className="batch__hint">Choose a spreadsheet to begin.</p>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------------
// 2 & 3. Running, and finished
// ---------------------------------------------------------------------------------

function BatchResults({
  status,
  isSample,
  decisions,
  onDecisions,
  filter,
  onFilter,
  openItem,
  onOpenItem,
  onCloseItem,
  onRetry,
  retrying,
  onStartOver,
  problem,
}: {
  status: BatchStatus;
  isSample: boolean;
  decisions: Record<string, Partial<Record<FieldName, AgentDecision>>>;
  onDecisions: (
    itemId: string,
    updated: Partial<Record<FieldName, AgentDecision>>,
  ) => void;
  filter: Filter;
  onFilter: (filter: Filter) => void;
  openItem: string | null;
  onOpenItem: (id: string | null) => void;
  onCloseItem: () => void;
  onRetry: () => void;
  retrying: boolean;
  onStartOver: () => void;
  problem: ApiError | null;
}) {
  const { counts, summary } = status;
  const done = status.state === 'done';
  const percent = percentDone(counts.done, counts.failed, counts.total);

  // Server order, never re-sorted here. `worst_first` is the contract; anything the
  // server did not rank goes after, in arrival order, so nothing silently disappears.
  const ordered = useMemo(() => {
    // Our own writes win over the poll's copy of them. Everything else about the item is
    // the server's — this overlays the one field this screen is allowed to author.
    const withDecisions = status.items.map((item) =>
      decisions[item.item_id] ? { ...item, decisions: decisions[item.item_id]! } : item,
    );
    const byId = new Map(withDecisions.map((item) => [item.item_id, item]));
    const ranked = summary.worst_first.flatMap((id) => byId.get(id) ?? []);
    const seen = new Set(ranked.map((item) => item.item_id));
    return [...ranked, ...status.items.filter((item) => !seen.has(item.item_id))];
  }, [status.items, summary.worst_first, decisions]);

  const visible = useMemo(
    () => (filter === 'all' ? ordered : ordered.filter((item) => bucketOf(item) === filter)),
    [ordered, filter],
  );

  const open = openItem ? (ordered.find((i) => i.item_id === openItem) ?? null) : null;

  return (
    <div className="batch">
      <header className="batch__intro">
        <h1 className="batch__title">
          {done ? 'Batch finished' : 'Checking your batch'}
        </h1>
        <p className="batch__lede" role="status">
          {status.message || summary.headline}
        </p>
      </header>

      {problem ? (
        <ProblemNote
          problem={problem}
          note="The batch is still running on the server — this page just could not reach it. It will keep trying."
        />
      ) : null}

      {!done ? (
        <section className="batch__progress" aria-label="Progress">
          <div
            className="progress"
            role="progressbar"
            aria-valuenow={percent}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label={`${counts.done + counts.failed} of ${counts.total} checked`}
          >
            <div className="progress__fill" style={{ width: `${percent}%` }} />
          </div>
          <p className="batch__eta">{formatEta(status.eta_seconds)}</p>
        </section>
      ) : null}

      {/* Every number here has to differ from the one beside it or it is not a number, it
          is noise. "Applications 6 / Checked 6" was two readings of one fact on every batch
          that finished cleanly — which is most of them — and a reviewer who learns that one
          pair means nothing reads the next pair less carefully. So Checked appears only
          while it disagrees with the total: mid-run, and on a finished job that lost rows
          to failures, where "6 applications, 5 checked, 1 could not be checked" is three
          facts. */}
      <dl className="batch__counts">
        <Count label="Applications" value={counts.total} />
        {counts.done !== counts.total ? <Count label="Checked" value={counts.done} /> : null}
        {counts.failed > 0 ? <Count label="Could not check" value={counts.failed} tone="serious" /> : null}
        {!done ? <Count label="Waiting" value={counts.queued + counts.processing} /> : null}
        {done && status.cost.usd > 0 ? (
          <Count label="Cost" value={`$${status.cost.usd.toFixed(2)}`} />
        ) : null}
      </dl>

      {status.row_errors.length > 0 ? (
        <RowErrors status={status} isSample={isSample} />
      ) : null}
      {status.unmatched_files.length > 0 ? (
        <details className="batch__notice">
          <summary>
            {status.unmatched_files.length} uploaded file
            {status.unmatched_files.length === 1 ? ' was' : 's were'} not named by any row
          </summary>
          <ul className="batch__filelist">
            {status.unmatched_files.map((name) => (
              <li key={name}>{name}</li>
            ))}
          </ul>
        </details>
      ) : null}

      <div className="batch__toolbar">
        <div className="filters" role="group" aria-label="Show">
          {FILTERS.map((entry) => {
            const n = countIn(ordered, entry.id);
            return (
              <button
                key={entry.id}
                type="button"
                className="filter"
                aria-pressed={filter === entry.id}
                disabled={n === 0 && entry.id !== 'all'}
                onClick={() => onFilter(entry.id)}
              >
                {entry.label} <span className="filter__count">{n}</span>
              </button>
            );
          })}
        </div>

        <div className="batch__actions">
          {counts.failed > 0 ? (
            <button
              type="button"
              className="btn"
              onClick={onRetry}
              disabled={retrying}
            >
              {retrying ? 'Retrying…' : `Retry ${counts.failed} that could not be checked`}
            </button>
          ) : null}
          {done ? (
            <a className="btn" href={batchExportUrl(status.job_id)} download>
              Export CSV
            </a>
          ) : null}
          <button type="button" className="btn" onClick={onStartOver}>
            Start another batch
          </button>
        </div>
      </div>

      <BatchTable items={visible} total={ordered.length} onOpen={onOpenItem} />

      {/* Keyed by item. The dialog seeds its decision state from the item ONCE and then
          trusts its own writes over the poll, which is what stops a stale poll from
          flipping a button back. That is only correct if opening a different application
          gives it a fresh instance — without the key React would reuse the one it has and
          the second row would open showing the first row's rulings. */}
      {open ? (
        <ItemDetail
          key={open.item_id}
          item={open}
          onClose={onCloseItem}
          onDecisions={(updated) => onDecisions(open.item_id, updated)}
        />
      ) : null}
    </div>
  );
}

function Count({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | string;
  tone?: 'serious';
}) {
  return (
    <div className="batch__count" data-tone={tone ?? undefined}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function RowErrors({ status, isSample }: { status: BatchStatus; isSample: boolean }) {
  const many = status.row_errors.length !== 1;
  return (
    <details className="batch__notice" data-tone="serious" open>
      <summary>
        {isSample
          ? `The sample includes ${status.row_errors.length} row${many ? 's' : ''} that cannot be used`
          : `${status.row_errors.length} row${many ? 's' : ''} in the spreadsheet could not be used`}
      </summary>
      {/* The sample's bad row is deliberate — it is there to show what a bad row looks
          like before a reviewer meets one in their own file. Telling them to "fix these
          rows" would be blaming them for a mistake we planted, and the first thing they
          would do is go looking for a spreadsheet they never uploaded.

          Both sentences end by saying these rows are not in the table. This notice names a
          manifest row, the table below is a list of manifest rows, and the one named here
          is the one missing from it — so without the sentence the notice reads as a row
          that went astray rather than a row that was never queued.

          Same column heading as the triage table, deliberately: one number, one name for
          it, or the reviewer has to work out whether they are the same kind of number. */}
      <p className="batch__notice-help">
        {isSample
          ? 'That is on purpose, so you can see how a bad row is reported: it is named by its row number and everything else was queued anyway. Nothing here is your mistake. A row listed here was never checked, so it does not appear in the results below.'
          : 'Everything else was queued. A row listed here was never checked, so it does not appear in the results below. Fix these rows and upload them as a second batch.'}
      </p>
      <table className="rowerrors">
        <thead>
          <tr>
            <th scope="col">Manifest row</th>
            <th scope="col">Column</th>
            <th scope="col">What is wrong</th>
          </tr>
        </thead>
        <tbody>
          {status.row_errors.map((err, i) => (
            <tr key={`${err.row}-${err.column ?? ''}-${i}`}>
              <td>{err.row}</td>
              <td>{err.column ?? '—'}</td>
              <td>{err.message}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

// ---------------------------------------------------------------------------------
// The triage table
// ---------------------------------------------------------------------------------

const STATE_WORDS: Record<ItemState, string> = {
  queued: 'Waiting',
  processing: 'Checking…',
  done: '',
  failed: 'Could not check',
};

function BatchTable({
  items,
  total,
  onOpen,
}: {
  items: BatchItem[];
  total: number;
  onOpen: (id: string) => void;
}) {
  if (total === 0) {
    return (
      <p className="batch__empty">
        No results yet. The first ones will appear here as they finish.
      </p>
    );
  }
  if (items.length === 0) {
    return (
      <p className="batch__empty">
        Nothing in this batch matched that filter. Choose <strong>All</strong> to see
        every application.
      </p>
    );
  }

  // The caption is visible now, and it exists to answer one question: why do the row
  // numbers run 4, 5, 6, 3, 2? Because the table is sorted worst-first — the point of the
  // whole screen — and the number is where the application sits in the spreadsheet, which
  // does not move. Neither half of that was on screen anywhere, so the column read as
  // scrambled, and a compliance queue that looks like it shuffled its input is one a
  // reviewer stops trusting. It stays a `<caption>` rather than a paragraph above the table
  // so a screen reader still hears it on entering the table, and so it survives the stacked
  // layout below 900px, where `thead` is hidden and the column headings go with it — there
  // the caption is the only thing on screen that says what the leading number is.
  return (
    <table className="triage">
      <caption className="triage__caption">
        Most serious first. <strong>Manifest row</strong> is where the application sits in
        the spreadsheet — row 1 is the column headings, so the first application is row 2.
        These run in order of seriousness, not in file order.
      </caption>
      <thead>
        <tr>
          <th scope="col">Manifest row</th>
          <th scope="col">Brand</th>
          <th scope="col">Recommendation</th>
          <th scope="col">What drove it</th>
          <th scope="col">
            <span className="visually-hidden">Open</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <TriageRow key={item.item_id} item={item} onOpen={onOpen} />
        ))}
      </tbody>
    </table>
  );
}

function TriageRow({ item, onOpen }: { item: BatchItem; onOpen: (id: string) => void }) {
  const recommendation = item.result?.aggregate.recommendation ?? null;
  const meta = recommendation ? RECOMMENDATIONS[recommendation] : null;
  const finished = item.state === 'done' || item.state === 'failed';

  const driving = item.result?.aggregate.driving_field ?? null;
  const worstVerdict = driving
    ? (item.result?.fields.find((f) => f.field === driving)?.verdict ?? null)
    : null;

  return (
    <tr data-state={item.state} data-recommendation={recommendation ?? undefined}>
      <td>{item.row}</td>
      <td>{item.application?.brand_name || <span className="muted">—</span>}</td>
      <td>
        {meta ? (
          <span className="chip" data-tone={meta.tone}>
            <VerdictGlyph icon={meta.icon} />
            {meta.word}
          </span>
        ) : (
          <span className="muted">{STATE_WORDS[item.state]}</span>
        )}
      </td>
      {/* "What drove it" used to go blank on every row that had no single field to blame,
          which is two quite different rows: the one where everything matched, and the one
          where the photograph could not be read at all. Both sat next to a neighbour
          carrying a full sentence, and a dash beside a paragraph reads as a value that
          failed to load rather than as an answer.

          So the cell always says something. A clean row says so in three words — it does
          not need the aggregate's sentence, which ends "the final decision is yours" on
          every one of them and turns into wallpaper. Anything else with no driving field
          gets the aggregate's own rationale, because that is where the reason lives: the
          blurred photograph in the sample batch says "the photo is too blurry to read the
          label. Retake it…", which is the whole content of that row.

          And it names the field. The verdict word alone answered "what drove it" with
          "Missing" — true of a label with no alcohol content and of an import with no
          country of origin alike, and the aggregate's sentence for both of them is the
          generic "a required element is not on the label". Two rows about two different
          rules read as the same row, in the one column whose job is to say which. */}
      <td>
        {item.failure ? (
          <span className="muted">{item.failure.message}</span>
        ) : driving && worstVerdict ? (
          <>
            {fieldLabel(driving)}: {VERDICTS[worstVerdict].word}
            {item.result?.aggregate.rationale ? (
              <span className="muted"> — {item.result.aggregate.rationale}</span>
            ) : null}
          </>
        ) : recommendation === 'ready_to_approve' ? (
          <span className="muted">Nothing needed attention.</span>
        ) : item.result ? (
          <span className="muted">{item.result.aggregate.rationale}</span>
        ) : (
          // Still queued or mid-check. The Recommendation column beside it already says
          // which, so a second copy of "Waiting" here would be the pair problem again.
          <span className="muted">—</span>
        )}
      </td>
      <td>
        {finished ? (
          <button type="button" className="link" onClick={() => onOpen(item.item_id)}>
            Open
            <span className="visually-hidden"> row {item.row}</span>
          </button>
        ) : null}
      </td>
    </tr>
  );
}

function ProblemNote({ problem, note }: { problem: ApiError | null; note?: string }) {
  return (
    <p className="batch__problem" role="alert">
      {problem?.message ?? 'Something went wrong and nothing was checked.'}
      {note ? ` ${note}` : ''}
    </p>
  );
}
