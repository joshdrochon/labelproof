/**
 * Verify Now — upload, enter, verdict. One screen, one primary action (UX-2).
 *
 * The screen has exactly four states and is only ever in one of them: **setup**,
 * **working**, **checked**, **problem**. In particular the left panel is a dropzone in
 * setup and an evidence viewer in checked, and never both — the mock had them fighting
 * over the same rectangle.
 *
 * The checklist is split by `attentionFields()` (see `triage.ts`). Rows that need a human
 * come first, at full weight, already open. Rows that agree stay on the page — it is a
 * checklist and agents expect to see every line — but they are quiet: smaller, greyer,
 * closed. The eye lands on the problem.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import type {
  AgentDecision,
  AgentEntry,
  ApiError,
  Application,
  FieldName,
  VerificationResult,
} from '../types';
import { ApiFailure, listSamples, loadSample, prepareReading, verify } from '../api';
import type { PreparedReading, SampleCase } from '../api';
import { NEXT_STEP_LABELS, STAGES } from '../copy';
import { evidenceRegions, numberFor, regionFor } from '../evidence';
import { attentionFields, settledFields } from '../triage';
import AggregateBanner from '../components/AggregateBanner';
import ApplicationForm, {
  EMPTY_DRAFT,
  draftFromApplication,
  toApplication,
  validateDraft,
} from '../components/ApplicationForm';
import type { ApplicationDraft, DraftProblems } from '../components/ApplicationForm';
import Dropzone from '../components/Dropzone';
import EvidenceOverlay from '../components/EvidenceOverlay';
import FieldRow from '../components/FieldRow';

type Phase = 'setup' | 'working' | 'checked' | 'problem';

interface Checked {
  result: VerificationResult;
  application: Application;
  /** The client's wall clock, submit to render. Recorded, not displayed — the card
   *  reports the server's total, which describes the work rather than the wait. */
  elapsedMs: number;
  imageUrls: string[];
  /**
   * Per picture: true where the outlines are drawn over the upload rather than a
   * server-side copy. Not one flag for the whole result — a server that names the front
   * and not the back would otherwise have both declared exact, and the back's outlines
   * would sit on the un-deskewed original with nothing said.
   */
  approximateByIndex: boolean[];
}

export default function VerifyNow() {
  const [phase, setPhase] = useState<Phase>('setup');
  const [draft, setDraft] = useState<ApplicationDraft>(EMPTY_DRAFT);
  const [files, setFiles] = useState<File[]>([]);
  const [problems, setProblems] = useState<DraftProblems>({});
  const [failure, setFailure] = useState<ApiError | null>(null);
  const [checked, setChecked] = useState<Checked | null>(null);

  const [stage, setStage] = useState(0);
  const [waited, setWaited] = useState(0);

  const [expanded, setExpanded] = useState<Set<FieldName>>(new Set());
  const [activeField, setActiveField] = useState<FieldName | null>(null);
  const [decisions, setDecisions] = useState<Partial<Record<FieldName, AgentDecision>>>({});
  const [determination, setDetermination] = useState<'approved' | 'returned' | null>(null);
  // What the agent read off the ARTWORK for rows the tool could not answer. Session
  // only, and never merged into the result — see the note on `AgentEntry`.
  const [entries, setEntries] = useState<Partial<Record<FieldName, AgentEntry>>>({});
  const [imageIndex, setImageIndex] = useState(0);
  // The demos on offer, fetched on mount so the setup screen can show what a reviewer
  // can try before they commit to a click. Empty until it answers, and empty forever if
  // it fails — manual entry is the rest of the screen and does not depend on this.
  const [samples, setSamples] = useState<SampleCase[]>([]);
  // A reading taken while the form was still being filled (LP-346). Null whenever there
  // is nothing usable — no files yet, still reading, or the attempt came back empty.
  // Every path that reaches `runCheck` works with or without it.
  //   null      — no attempt has finished for the current files
  //   {token}   — the label has been read and the reading is usable
  //   'none'    — an attempt finished and produced nothing usable
  //
  // One piece of state, written only when an attempt SETTLES. An earlier version kept a
  // separate in-flight boolean written around the call, and the effect's own cleanup
  // reset it faster than any render could show it.
  const [reading, setReading] = useState<PreparedReading | 'none' | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const localUrlsRef = useRef<string[]>([]);
  const startedRef = useRef(0);

  useEffect(() => {
    if (phase !== 'working') return undefined;
    // Narration starts inside a second and keeps moving — never a dead spinner (PERF-7).
    setStage(0);
    setWaited(0);
    const started = Date.now();
    const tick = window.setInterval(() => {
      const elapsed = Date.now() - started;
      setWaited(elapsed);
      setStage(Math.min(STAGES.length - 1, Math.floor(elapsed / 900)));
    }, 200);
    return () => window.clearInterval(tick);
  }, [phase]);

  useEffect(
    () => () => {
      for (const url of localUrlsRef.current) URL.revokeObjectURL(url);
    },
    [],
  );

  const settle = useCallback(
    (result: VerificationResult, application: Application, imageUrls: string[]) => {
      const serverUrls = result.images
        .slice()
        .sort((a, b) => a.index - b.index)
        .map((image) => image.url ?? null);
      const hasServerUrls = serverUrls.some((url) => Boolean(url));
      const urls = hasServerUrls
        ? serverUrls.map((url, i) => url ?? imageUrls[i] ?? '')
        : imageUrls;

      setChecked({
        result,
        application,
        elapsedMs: Date.now() - startedRef.current,
        imageUrls: urls,
        approximateByIndex: urls.map((_, i) => !serverUrls[i]),
      });
      setExpanded(new Set(attentionFields(result.fields).map((row) => row.field)));
      setDecisions({});
      setDetermination(null);
      setActiveField(null);
      setImageIndex(0);
      setPhase('checked');
    },
    [],
  );

  const localPreviewUrls = useCallback((forFiles: File[]) => {
    for (const url of localUrlsRef.current) URL.revokeObjectURL(url);
    localUrlsRef.current = forFiles
      .filter((file) => file.type.startsWith('image/'))
      .map((file) => URL.createObjectURL(file));
    return localUrlsRef.current;
  }, []);

  const runCheck = useCallback(async () => {
    const found = validateDraft(draft, files.length);
    if (Object.keys(found).length > 0) {
      // `flushSync`, because this used to call `setProblems` and then read the DOM on the
      // very next line. React had not re-rendered yet, so `[aria-invalid="true"]` matched
      // NOTHING on the first submit and focus never moved — a keyboard user pressed the
      // button, heard nothing, and had no way to know five fields below had just been
      // marked. It looked correct in manual testing only because the second submit finds
      // the marks the first one left behind.
      //
      // The jsdom suite could not catch this. It asserted the fields were marked, which
      // they were. A real browser found it on the first run.
      flushSync(() => setProblems(found));
      document.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
      return;
    }
    setProblems(found);
    const application = toApplication(draft);
    const previews = localPreviewUrls(files);
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    startedRef.current = Date.now();
    setFailure(null);
    setEntries({});
    setPhase('working');
    try {
      const result = await verify(
        {
          application,
          files,
          preparedToken: reading && reading !== 'none' ? reading.token : undefined,
        },
        controller.signal,
      );
      settle(result, application, previews);
    } catch (error) {
      if ((error as Error)?.name === 'AbortError') return;
      setFailure(
        error instanceof ApiFailure
          ? error.detail
          : {
              kind: 'internal',
              code: 'unknown',
              message: 'Something went wrong and nothing was checked.',
              next_step: 'retry',
            },
      );
      setPhase('problem');
    }
  }, [draft, files, localPreviewUrls, settle, reading]);

  useEffect(() => {
    const controller = new AbortController();
    void listSamples(controller.signal).then(setSamples);
    return () => controller.abort();
  }, []);

  // Read the label as soon as there is one, and re-read if the pictures or the kind of
  // product change — a reading is of specific bytes under a specific rule set, and the
  // server refuses a token that does not match on both. Aborting on change matters: a
  // superseded read must not land after the one that replaced it.
  useEffect(() => {
    setReading(null);
    if (files.length === 0 || phase === 'working') return undefined;
    const controller = new AbortController();
    // A flag per run, rather than reading `controller.signal.aborted` back. React runs
    // effects twice in development — setup, cleanup, setup — so the signal belonging to
    // a superseded run is aborted while the run that replaced it is the live one. Keying
    // the state update to the signal meant the surviving request's result was discarded
    // and the panel never said anything, in development only.
    let live = true;
    void prepareReading(files, draft.commodity, controller.signal).then((prepared) => {
      if (live) setReading(prepared ?? 'none');
    });
    return () => {
      live = false;
      controller.abort();
    };
    // `phase` is deliberately absent: re-reading because the screen moved to `checked`
    // would spend a model call on a label that has already been answered.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [files, draft.commodity]);

  const runSample = useCallback(async (slug?: string) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    startedRef.current = Date.now();
    setProblems({});
    setFailure(null);
    setPhase('working');
    try {
      const outcome = await loadSample(slug, controller.signal);
      const application =
        outcome.application ??
        ({
          ...toApplication(EMPTY_DRAFT),
          brand_name: 'Sample application',
        } as Application);
      if (outcome.application) setDraft(draftFromApplication(outcome.application));
      if (outcome.cases.length) setSamples(outcome.cases);
      settle(
        outcome.result,
        application,
        outcome.images.map((image) => image.url),
      );
    } catch (error) {
      if ((error as Error)?.name === 'AbortError') return;
      setFailure(
        error instanceof ApiFailure
          ? error.detail
          : {
              kind: 'internal',
              code: 'unknown',
              message: 'The sample could not be loaded.',
              next_step: 'retry',
            },
      );
      setPhase('problem');
    }
  }, [settle]);

  /**
   * Go back for a better picture, keeping everything the agent typed.
   *
   * Distinct from `startOver`, which clears the application too. An Unreadable row is
   * almost always a photograph problem, and making someone re-enter seven fields to
   * re-shoot one image is how a tool earns the reputation the previous vendor has.
   */
  const retake = useCallback(() => {
    abortRef.current?.abort();
    for (const url of localUrlsRef.current) URL.revokeObjectURL(url);
    localUrlsRef.current = [];
    setPhase('setup');
    setChecked(null);
    setFiles([]);
    setFailure(null);
    setDecisions({});
    setDetermination(null);
    setEntries({});
  }, []);

  const startOver = useCallback(() => {
    abortRef.current?.abort();
    for (const url of localUrlsRef.current) URL.revokeObjectURL(url);
    localUrlsRef.current = [];
    setPhase('setup');
    setChecked(null);
    setFiles([]);
    setDraft(EMPTY_DRAFT);
    setProblems({});
    setFailure(null);
    setDecisions({});
    setDetermination(null);
    setEntries({});
  }, []);

  if (phase === 'checked' && checked) {
    return (
      <ChecklistScreen
        checked={checked}
        expanded={expanded}
        setExpanded={setExpanded}
        activeField={activeField}
        setActiveField={setActiveField}
        decisions={decisions}
        setDecisions={setDecisions}
        determination={determination}
        setDetermination={setDetermination}
        entries={entries}
        setEntries={setEntries}
        onRetake={retake}
        imageIndex={imageIndex}
        setImageIndex={setImageIndex}
        onStartOver={startOver}
      />
    );
  }

  return (
    <div className="setup">
      <div className="setup__intro">
        <h1 className="setup__title">Check a label against its application</h1>
        <ol className="steps">
          <li className="step">
            <span className="step__number">1</span>
            <span className="step__text">Add pictures of the label.</span>
          </li>
          <li className="step">
            <span className="step__number">2</span>
            <span className="step__text">Type what the application says.</span>
          </li>
          <li className="step">
            <span className="step__number">3</span>
            <span className="step__text">Read the checklist and decide.</span>
          </li>
        </ol>
        <div className="setup__sample">
          <p className="setup__sample-note">
            Or try one of these. Each one loads a real application and its label and
            checks it right away — nothing to fill in.
          </p>

          {/* One card per sample, the WHOLE card clickable. This was a `.btn` with the
              summary running beside it, so four buttons of four different widths gave
              four different text starts and the block read as rubble — and the target
              was the words rather than the card. */}
          {samples.length > 0 ? (
            <ul className="samples">
              {samples.map((sample, index) => (
                <li key={sample.slug}>
                  <button
                    type="button"
                    className="sample"
                    data-lead={index === 0 ? 'true' : 'false'}
                    onClick={() => void runSample(sample.slug)}
                    disabled={phase === 'working'}
                  >
                    <span className="sample__title">{sample.title}</span>
                    <span className="sample__summary">{sample.summary}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <button
              type="button"
              className="btn btn--secondary"
              onClick={() => void runSample()}
              disabled={phase === 'working'}
            >
              Try a sample
            </button>
          )}
        </div>
      </div>

      {phase === 'problem' && failure ? (
        <ProblemPanel
          failure={failure}
          onRetry={files.length > 0 ? runCheck : runSample}
          onStartOver={startOver}
        />
      ) : null}

      <div className="setup__columns">
        <div className="panel">
          {phase === 'working' ? (
            <WorkingPanel stage={stage} waited={waited} />
          ) : (
            <>
              <Dropzone files={files} onChange={setFiles} />
              {problems.images ? (
                <p className="dropzone__problem" role="alert">
                  {problems.images}
                </p>
              ) : null}
              {/* Quiet on purpose. The agent did not ask for this and cannot act on it;
                  it is here so a head start is visible rather than mysterious, and so a
                  fast result later is not surprising. `role="status"` and not `alert` —
                  nothing here needs interrupting. */}
              {files.length > 0 && reading !== 'none' ? (
                <p className="reading-ahead" role="status">
                  {reading === null
                    ? 'Reading the label while you fill in the details…'
                    : 'Label read. Checking will be quick.'}
                </p>
              ) : null}
            </>
          )}
        </div>

        <div className="panel">
          <ApplicationForm
            draft={draft}
            onChange={setDraft}
            problems={problems}
            disabled={phase === 'working'}
          />
          <div className="setup__actions">
            <button
              type="button"
              className="btn btn--primary"
              onClick={runCheck}
              disabled={phase === 'working'}
            >
              {phase === 'working' ? 'Checking…' : 'Check this label'}
            </button>
            <p className="setup__reassurance">
              Nothing is filed and nothing is sent to the applicant. This screen only
              compares the two and hands you a checklist.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------------------
// Working
// -----------------------------------------------------------------------------------

function WorkingPanel({ stage, waited }: { stage: number; waited: number }) {
  const seconds = Math.floor(waited / 1000);
  return (
    <div className="working" role="status" aria-live="polite">
      <h2 className="working__title">Checking the label</h2>
      <ol className="working__stages">
        {STAGES.map((text, index) => (
          <li
            key={text}
            className="working__stage"
            data-state={index < stage ? 'done' : index === stage ? 'now' : 'waiting'}
          >
            <span className="working__stage-mark" aria-hidden="true" />
            <span className="working__stage-text">{text}</span>
          </li>
        ))}
      </ol>
      <p className="working__elapsed">
        {seconds < 1 ? 'Started' : `${seconds} second${seconds === 1 ? '' : 's'} so far`}
      </p>
      <div className="skeleton" aria-hidden="true">
        {[0, 1, 2, 3, 4].map((row) => (
          <span className="skeleton__row" key={row} />
        ))}
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------------------
// Problem
// -----------------------------------------------------------------------------------

function ProblemPanel({
  failure,
  onRetry,
  onStartOver,
}: {
  failure: ApiError;
  onRetry: () => void;
  onStartOver: () => void;
}) {
  const nextStep = failure.next_step ?? null;
  return (
    <section className="problem" role="alert">
      <h2 className="problem__title">Nothing was checked</h2>
      <p className="problem__message">{failure.message}</p>
      <div className="problem__actions">
        {nextStep === 'retake' ? (
          <p className="problem__hint">
            {NEXT_STEP_LABELS['retake']} from the applicant, or photograph the bottle
            again without flash.
          </p>
        ) : (
          <button type="button" className="btn btn--secondary" onClick={onRetry}>
            Try again
          </button>
        )}
        <button type="button" className="btn btn--quiet" onClick={onStartOver}>
          Start a new check
        </button>
      </div>
    </section>
  );
}

// -----------------------------------------------------------------------------------
// Checked
// -----------------------------------------------------------------------------------

interface ChecklistScreenProps {
  checked: Checked;
  expanded: Set<FieldName>;
  setExpanded: (value: Set<FieldName>) => void;
  activeField: FieldName | null;
  setActiveField: (field: FieldName | null) => void;
  decisions: Partial<Record<FieldName, AgentDecision>>;
  setDecisions: (value: Partial<Record<FieldName, AgentDecision>>) => void;
  determination: 'approved' | 'returned' | null;
  setDetermination: (value: 'approved' | 'returned' | null) => void;
  entries: Partial<Record<FieldName, AgentEntry>>;
  setEntries: (value: Partial<Record<FieldName, AgentEntry>>) => void;
  /** Back to the pictures, with the application details kept. */
  onRetake: () => void;
  imageIndex: number;
  setImageIndex: (index: number) => void;
  onStartOver: () => void;
}

function ChecklistScreen({
  checked,
  expanded,
  setExpanded,
  activeField,
  setActiveField,
  decisions,
  setDecisions,
  determination,
  setDetermination,
  entries,
  setEntries,
  onRetake,
  imageIndex,
  setImageIndex,
  onStartOver,
}: ChecklistScreenProps) {
  const { result, application, imageUrls, approximateByIndex } = checked;
  const attention = useMemo(() => attentionFields(result.fields), [result.fields]);
  const settled = useMemo(() => settledFields(result.fields), [result.fields]);

  // Built by `evidence.ts`, which the batch drill-in also calls. Inlining it here again
  // would let the two screens number the same label differently.
  const regions = useMemo(() => evidenceRegions(result.fields), [result.fields]);


  const toggle = useCallback(
    (field: FieldName) => {
      const next = new Set(expanded);
      if (next.has(field)) next.delete(field);
      else next.add(field);
      setExpanded(next);
    },
    [expanded, setExpanded],
  );

  /**
   * Turn to the picture a row's outline is actually on.
   *
   * **Deliberate actions only** — the banner's row links and the n/p/j/k keys, both of
   * which are an agent saying "take me to this row". It is NOT wired to hover.
   *
   * It was, briefly, and driving the screen showed why that is wrong: `FieldRow.onActivate`
   * fires on `mouseenter` and `focus`, so sweeping the mouse down the checklist flipped
   * the picture to the back and left it there — `mouseleave` passes null, which names no
   * region and so restores nothing. Worse, it overrode the agent: choosing "back" on the
   * switcher and then reading any front row snapped the picture away, making a two-sided
   * label impossible to hold still. Hover is a HIGHLIGHT, confined to the picture already
   * on screen and fully reverted on leave (LP-104); turning the page is a decision.
   */
  const showRegionFor = useCallback(
    (field: FieldName) => {
      const region = regionFor(regions, field);
      if (!region) return;
      // Clamped, because `image_index` arrives from the server and nothing upstream
      // promises it names a picture this result actually has. Unclamped it set an index
      // with no URL behind it: the panel fell back to "not available to display" and the
      // switcher — which only renders for two or more pictures — was not there to undo
      // it. The label was gone for the session, recoverable only by starting over and
      // discarding every decision on the screen.
      if (region.imageIndex >= 0 && region.imageIndex < imageUrls.length) {
        setImageIndex(region.imageIndex);
      }
    },
    [regions, imageUrls.length, setImageIndex],
  );

  const jumpToField = useCallback(
    (field: FieldName) => {
      if (!expanded.has(field)) toggle(field);
      setActiveField(field);
      showRegionFor(field);
      const row = document.getElementById(`row-${field}`);
      row?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      row?.querySelector<HTMLElement>('.row__toggle')?.focus();
    },
    [expanded, setActiveField, showRegionFor, toggle],
  );

  // Keyboard shortcuts for the power user (UX-4, LP-111). Never while typing.
  useEffect(() => {
    const order = [...attention, ...settled];
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (
        event.metaKey ||
        event.ctrlKey ||
        event.altKey ||
        (target &&
          (target.tagName === 'INPUT' ||
            target.tagName === 'TEXTAREA' ||
            target.tagName === 'SELECT'))
      ) {
        return;
      }
      const current = activeField ?? order[0]?.field ?? null;
      const index = order.findIndex((row) => row.field === current);
      if (event.key === 'n' || event.key === 'j') {
        const next = order[Math.min(order.length - 1, index + 1)] ?? order[0];
        if (next) jumpToField(next.field);
        event.preventDefault();
      } else if (event.key === 'p' || event.key === 'k') {
        const prev = order[Math.max(0, index - 1)] ?? order[0];
        if (prev) jumpToField(prev.field);
        event.preventDefault();
        // `a` and `d`, matching the words on the buttons. They were `c` and `o` — for
        // "confirm" and "override", the vocabulary this UI deliberately does not use.
        // A shortcut hint that teaches keys spelling words nobody can see on the screen
        // is a hint for whoever wrote the handler. `c`/`o` still work for anyone who
        // learned them, they are just not what the page advertises.
      } else if ('adco'.includes(event.key) && current) {
        const agreeing = event.key === 'a' || event.key === 'c';
        setDecisions({ ...decisions, [current]: agreeing ? 'confirmed' : 'overridden' });
        event.preventDefault();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [attention, settled, activeField, decisions, jumpToField, setDecisions]);

  const activeImage = result.images.find((image) => image.index === imageIndex);
  const qualityNote =
    activeImage && activeImage.quality.verdict !== 'ok'
      ? (activeImage.quality.reason ??
        'This picture was hard to read. Anything marked Unreadable needs a better image or your own eyes on the bottle.')
      : null;

  const productName =
    [application.brand_name, application.class_type].filter(Boolean).join(' — ') ||
    'This application';

  return (
    <div className="checked">
      <header className="checked__header">
        <div className="checked__identity">
          <p className="checked__reference">
            <span className="checked__reference-label">Check reference</span>
            <span className="checked__reference-value">{result.request_id || '—'}</span>
            {/* OPS-1 asks for elapsed time on every result card, and it stays — but here,
                with the reference, rather than beside the recommendation. Once the label
                is read while the form is filled the number stops describing anything the
                agent waited for, and a verdict is not the place for a statistic nobody
                acted on. It is the WORK, never the wait. */}
            {result.timings_ms.total ? (
              <span className="checked__elapsed" data-testid="elapsed">
                Checked in {(result.timings_ms.total / 1000).toFixed(1)}s
              </span>
            ) : null}
          </p>
          <h1 className="checked__product">{productName}</h1>
        </div>
        <div className="checked__header-actions">
          <button type="button" className="btn btn--secondary" onClick={() => window.print()}>
            Print this checklist
          </button>
          <button type="button" className="btn btn--quiet" onClick={onStartOver}>
            Start a new check
          </button>
        </div>
      </header>

      <p className="visually-hidden" role="status" aria-live="polite">
        {`Recommendation: ${result.aggregate.recommendation.replace(/_/g, ' ')}. ${
          attention.length
        } of ${result.fields.length} rows need review.`}
      </p>

      <AggregateBanner
        aggregate={result.aggregate}
        fields={result.fields}
        onJumpToField={jumpToField}
      />

      <div className="checked__columns">
        <div className="checked__evidence">
          <EvidenceOverlay
            imageUrl={imageUrls[imageIndex] || null}
            imageIndex={imageIndex}
            imageLabel={
              activeImage?.role
                ? `Label picture — ${activeImage.role}`
                : `Label picture ${imageIndex + 1}`
            }
            regions={regions}
            activeField={activeField}
            onActivateField={setActiveField}
            geometryIsApproximate={approximateByIndex[imageIndex] ?? true}
            qualityNote={qualityNote}
          >
            {imageUrls.length > 1 ? (
              <div className="evidence__switch" role="group" aria-label="Which picture to show">
                {imageUrls.map((_, index) => {
                  const report = result.images.find((image) => image.index === index);
                  return (
                    <button
                      type="button"
                      key={index}
                      className="btn btn--quiet"
                      aria-pressed={index === imageIndex}
                      onClick={() => setImageIndex(index)}
                    >
                      {report?.role ?? `Picture ${index + 1}`}
                    </button>
                  );
                })}
              </div>
            ) : null}
          </EvidenceOverlay>
        </div>

        <div className="checked__checklist">
          <table className="checklist">
            <caption className="visually-hidden">
              Field by field comparison of the label against the application.
            </caption>
            <thead>
              <tr>
                <th scope="col">Field</th>
                <th scope="col">The application says</th>
                <th scope="col">The label shows</th>
                <th scope="col">Verdict</th>
              </tr>
            </thead>

            {attention.length > 0 ? (
              <tbody className="checklist__group" data-group="attention">
                <tr className="group-heading">
                  <th colSpan={4} scope="colgroup">
                    <span className="group-heading__text">
                      {attention.length === 1
                        ? '1 row needs your eyes'
                        : `${attention.length} rows need your eyes`}
                    </span>
                  </th>
                </tr>
                {attention.map((row) => (
                  <FieldRow
                    key={row.field}
                    result={row}
                    commodity={application.commodity}
                    variant="attention"
                    number={numberFor(regions, row.field)}
                    expanded={expanded.has(row.field)}
                    onToggle={() => toggle(row.field)}
                    onActivate={(on) => setActiveField(on ? row.field : null)}
                    decision={decisions[row.field] ?? null}
                    onDecide={(value) =>
                      setDecisions({ ...decisions, [row.field]: value ?? undefined })
                    }
                    entry={entries[row.field] ?? null}
                    onEnter={(value) =>
                      setEntries({ ...entries, [row.field]: value ?? undefined })
                    }
                    onRetake={onRetake}
                    isFocused={activeField === row.field}
                  />
                ))}
              </tbody>
            ) : null}

            {settled.length > 0 ? (
              <tbody className="checklist__group" data-group="settled">
                <tr className="group-heading group-heading--quiet">
                  <th colSpan={4} scope="colgroup">
                    <span className="group-heading__text">
                      {`${settled.length} checked and matching — nothing to do`}
                    </span>
                  </th>
                </tr>
                {settled.map((row) => (
                  <FieldRow
                    key={row.field}
                    result={row}
                    commodity={application.commodity}
                    variant="settled"
                    number={null}
                    expanded={expanded.has(row.field)}
                    onToggle={() => toggle(row.field)}
                    onActivate={(on) => setActiveField(on ? row.field : null)}
                    decision={decisions[row.field] ?? null}
                    onDecide={(value) =>
                      setDecisions({ ...decisions, [row.field]: value ?? undefined })
                    }
                    entry={entries[row.field] ?? null}
                    onEnter={(value) =>
                      setEntries({ ...entries, [row.field]: value ?? undefined })
                    }
                    onRetake={onRetake}
                    isFocused={activeField === row.field}
                  />
                ))}
              </tbody>
            ) : null}
          </table>

          {/* Folded away by default. It was a permanent band across the bottom of the
              checklist, which puts a power-user affordance in front of the person the
              73-year-old benchmark is about. Jenny finds it in one click; nobody else
              has to read it. */}
          <details className="checklist__shortcuts">
            <summary>Keyboard shortcuts</summary>
            <p>
              <kbd>n</kbd> next row · <kbd>p</kbd> previous · <kbd>a</kbd> agree ·{' '}
              <kbd>d</kbd> disagree
            </p>
          </details>
        </div>
      </div>

      <footer className="determination">
        <h2 className="determination__title">Your determination</h2>
        <p className="determination__text">
          The recommendation above is advice. What happens to this application is your
          call, and nothing here is filed anywhere.
        </p>
        <div className="determination__actions">
          <button
            type="button"
            className="btn btn--primary"
            aria-pressed={determination === 'approved'}
            onClick={() => setDetermination(determination === 'approved' ? null : 'approved')}
          >
            Approve
          </button>
          <button
            type="button"
            className="btn btn--secondary"
            aria-pressed={determination === 'returned'}
            onClick={() => setDetermination(determination === 'returned' ? null : 'returned')}
          >
            Return for correction
          </button>
        </div>
        {determination ? (
          <p className="determination__state" role="status">
            {determination === 'approved'
              ? 'You marked this Approved. Print the checklist for the case file.'
              : 'You marked this Return for correction. Print the checklist for the case file.'}
          </p>
        ) : null}
      </footer>
    </div>
  );
}
