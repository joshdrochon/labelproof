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
import type {
  AgentDecision,
  AgentEntry,
  ApiError,
  Application,
  FieldName,
  FieldResult,
  VerificationResult,
} from '../types';
import { ApiFailure, loadSample, verify } from '../api';
import { NEXT_STEP_LABELS, STAGES, fieldLabel } from '../copy';
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
import type { EvidenceRegion } from '../components/EvidenceOverlay';
import FieldRow from '../components/FieldRow';

type Phase = 'setup' | 'working' | 'checked' | 'problem';

interface Checked {
  result: VerificationResult;
  application: Application;
  elapsedMs: number;
  imageUrls: string[];
  /** True when the outlines are drawn over the upload rather than a server-side copy. */
  approximateGeometry: boolean;
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
        approximateGeometry: !hasServerUrls,
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
    setProblems(found);
    if (Object.keys(found).length > 0) {
      const first = document.querySelector<HTMLElement>('[aria-invalid="true"]');
      first?.focus();
      return;
    }
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
      const result = await verify({ application, files }, controller.signal);
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
  }, [draft, files, localPreviewUrls, settle]);

  const runSample = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    startedRef.current = Date.now();
    setProblems({});
    setFailure(null);
    setPhase('working');
    try {
      const outcome = await loadSample(controller.signal);
      const application =
        outcome.application ??
        ({
          ...toApplication(EMPTY_DRAFT),
          brand_name: 'Sample application',
        } as Application);
      if (outcome.application) setDraft(draftFromApplication(outcome.application));
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
          <button
            type="button"
            className="btn btn--secondary"
            onClick={runSample}
            disabled={phase === 'working'}
          >
            Try a sample
          </button>
          <span className="setup__sample-note">
            Loads a real application and its label, and checks it straight away. Nothing to
            fill in.
          </span>
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
  const { result, application, elapsedMs, imageUrls, approximateGeometry } = checked;
  const attention = useMemo(() => attentionFields(result.fields), [result.fields]);
  const settled = useMemo(() => settledFields(result.fields), [result.fields]);

  const regions = useMemo<EvidenceRegion[]>(() => {
    let counter = 0;
    const build = (row: FieldResult, needsAttention: boolean): EvidenceRegion[] => {
      const bbox = row.evidence?.bbox;
      // No box, no highlight. Never a guessed region.
      if (!bbox) return [];
      if (needsAttention) counter += 1;
      return [
        {
          field: row.field,
          label: fieldLabel(row.field),
          bbox,
          imageIndex: row.evidence?.image_index ?? 0,
          number: needsAttention ? counter : null,
          needsAttention,
        },
      ];
    };
    return [
      ...attention.flatMap((row) => build(row, true)),
      ...settled.flatMap((row) => build(row, false)),
    ];
  }, [attention, settled]);

  const numberFor = useCallback(
    (field: FieldName) => regions.find((r) => r.field === field)?.number ?? null,
    [regions],
  );

  const toggle = useCallback(
    (field: FieldName) => {
      const next = new Set(expanded);
      if (next.has(field)) next.delete(field);
      else next.add(field);
      setExpanded(next);
    },
    [expanded, setExpanded],
  );

  const jumpToField = useCallback(
    (field: FieldName) => {
      if (!expanded.has(field)) toggle(field);
      setActiveField(field);
      const row = document.getElementById(`row-${field}`);
      row?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      row?.querySelector<HTMLElement>('.row__toggle')?.focus();
    },
    [expanded, setActiveField, toggle],
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
      } else if ((event.key === 'c' || event.key === 'o') && current) {
        setDecisions({ ...decisions, [current]: event.key === 'c' ? 'confirmed' : 'overridden' });
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
        elapsedMs={elapsedMs}
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
            geometryIsApproximate={approximateGeometry}
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
                    number={numberFor(row.field)}
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

          <p className="checklist__shortcuts">
            Keyboard: <kbd>n</kbd> next row, <kbd>p</kbd> previous, <kbd>c</kbd> agree,{' '}
            <kbd>o</kbd> disagree.
          </p>
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
