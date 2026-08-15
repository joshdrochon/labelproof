/**
 * What the applicant filed — the reference side of every comparison.
 *
 * Written for someone pasting out of a COLA screen (LP-097), which means:
 *
 *   - Nothing is masked or reformatted while you type. Masks fight paste, and an agent
 *     who pastes `45.0% ABV` into the alcohol box should not be told off — that value is
 *     read for its number and kept.
 *   - Whitespace is trimmed on the way out, never on the way in, so the cursor never
 *     jumps mid-edit.
 *   - Labels sit above their inputs, in full words, at full size. Placeholder text is an
 *     example, never the label — it disappears exactly when a 73-year-old needs it.
 *   - Country of origin appears only when the application is an import, because an empty
 *     box that does not apply is one more thing to wonder about.
 */

import type { Application, Commodity } from '../types';
import { COMMODITY_LABELS } from '../copy';

export interface ApplicationDraft {
  commodity: Commodity;
  brand_name: string;
  class_type: string;
  alcohol_content: string;
  net_contents: string;
  producer_name: string;
  producer_address: string;
  country_of_origin: string;
  is_import: boolean;
}

export const EMPTY_DRAFT: ApplicationDraft = {
  commodity: 'spirits',
  brand_name: '',
  class_type: '',
  alcohol_content: '',
  net_contents: '',
  producer_name: '',
  producer_address: '',
  country_of_origin: '',
  is_import: false,
};

export function draftFromApplication(application: Application): ApplicationDraft {
  return {
    commodity: application.commodity,
    brand_name: application.brand_name,
    class_type: application.class_type,
    alcohol_content:
      application.alcohol_content === null ? '' : String(application.alcohol_content),
    net_contents: application.net_contents,
    producer_name: application.producer_name,
    producer_address: application.producer_address,
    country_of_origin: application.country_of_origin ?? '',
    is_import: application.is_import,
  };
}

/**
 * Reads ONE alcohol content out of a typed entry — the mirror of `api/entry.py`.
 *
 * The previous version was `raw.match(/-?\d+(\.\d+)?/)`: first number wins. That is the
 * right rule for reading a label and the wrong rule for reading a person, and it filed
 *
 *     45% BY VOL. (Front label) / 43% BY VOL. (Back label)
 *
 * as 45.0 with no message. A silent guess, in the first box on the first screen, in a
 * tool whose entire argument is that it never guesses.
 *
 * Returns `{ value }` when the entry names exactly one figure, `{ problem }` when it
 * names none or several. The server enforces the same rule and is the authority; this
 * exists so the agent hears about it before a request goes out, not after.
 */
export type EntryReading = { value: number | null; problem?: undefined } | { problem: string };

export function readAlcoholContent(raw: string): EntryReading {
  const text = raw.trim();
  if (!text) return { value: null };

  const numbers = (pattern: RegExp) =>
    [...text.matchAll(pattern)].map((m) => Number.parseFloat((m[1] ?? '').replace(',', '.')));

  const percents = numbers(/(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:%|percent\b)/gi);
  const proofs = numbers(/(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:proof\b|°)/gi).map((p) => p / 2);
  // Precedence, not union: `45% Alc./Vol. (90 Proof)` states one value twice.
  const found = percents.length ? percents : proofs.length ? proofs : numbers(/(\d{1,3}(?:[.,]\d{1,2})?)/g);

  const values = [...new Set(found.filter((n) => Number.isFinite(n)))].sort((a, b) => a - b);
  const [only] = values;
  if (only === undefined)
    return { problem: 'Enter the alcohol content as a number, for example "45".' };
  if (values.length > 1)
    return {
      problem:
        `This says ${values.join(' and ')}. Enter only the figure the application ` +
        `declares — this tool will not choose between them.`,
    };
  if (only < 0 || only > 100)
    return { problem: 'Alcohol content is a percentage by volume, between 0 and 100.' };
  return { value: only };
}

/** Two different sizes in the SAME unit is ambiguous; `750 mL (25.4 fl oz)` is not. */
export function readNetContents(raw: string): { problem?: string } {
  const byUnit = new Map<string, Set<number>>();
  const pattern =
    /(\d{1,5}(?:[.,]\d{1,3})?)\s*(ml|milliliters?|millilitres?|cl|centiliters?|centilitres?|l|liters?|litres?|fl\.?\s*oz\.?|fluid\s+ounces?|oz\.?)\b/gi;
  for (const match of raw.matchAll(pattern)) {
    const [, rawAmount = '', rawUnit = ''] = match;
    const unit = rawUnit.replace(/[.\s]/g, '').toLowerCase().replace(/s$/, '');
    const amount = Number.parseFloat(rawAmount.replace(',', '.'));
    if (!byUnit.has(unit)) byUnit.set(unit, new Set());
    byUnit.get(unit)!.add(amount);
  }
  for (const [unit, amounts] of byUnit) {
    if (amounts.size > 1)
      return {
        problem:
          `This says ${[...amounts].sort((a, b) => a - b).join(' and ')} ${unit}. Enter ` +
          `only the size the application declares.`,
      };
  }
  return {};
}

/** Back-compatible reader for callers that only need the number. */
export function parseAlcoholContent(raw: string): number | null {
  const reading = readAlcoholContent(raw);
  return 'problem' in reading ? null : reading.value;
}

export function toApplication(draft: ApplicationDraft): Application {
  const trim = (value: string) => value.trim();
  return {
    commodity: draft.commodity,
    brand_name: trim(draft.brand_name),
    class_type: trim(draft.class_type),
    alcohol_content: parseAlcoholContent(draft.alcohol_content),
    net_contents: trim(draft.net_contents),
    producer_name: trim(draft.producer_name),
    producer_address: trim(draft.producer_address),
    country_of_origin: draft.is_import ? trim(draft.country_of_origin) || null : null,
    is_import: draft.is_import,
  };
}

export type DraftProblems = Partial<Record<keyof ApplicationDraft | 'images', string>>;

/** Plain sentences, each naming the box and what to put in it. */
export function validateDraft(draft: ApplicationDraft, imageCount: number): DraftProblems {
  const problems: DraftProblems = {};
  if (!draft.brand_name.trim()) problems.brand_name = 'Enter the brand name from the application.';
  if (!draft.class_type.trim())
    problems.class_type = 'Enter the class or type, for example "Kentucky Straight Bourbon Whiskey".';
  if (!draft.net_contents.trim())
    problems.net_contents = 'Enter the net contents, for example "750 mL".';
  if (!draft.producer_name.trim()) problems.producer_name = 'Enter the producer or bottler name.';
  if (!draft.producer_address.trim())
    problems.producer_address = 'Enter the producer city and state.';
  if (draft.is_import && !draft.country_of_origin.trim())
    problems.country_of_origin = 'Imports need a country of origin.';
  const alcohol = readAlcoholContent(draft.alcohol_content);
  if ('problem' in alcohol) problems.alcohol_content = alcohol.problem;
  // Only when the box has something in it. An empty net contents is already caught above
  // as required, and reporting both would put two sentences under one box.
  if (draft.net_contents.trim()) {
    const size = readNetContents(draft.net_contents);
    if (size.problem) problems.net_contents = size.problem;
  }
  if (imageCount === 0) problems.images = 'Add at least one picture of the label.';
  return problems;
}

interface ApplicationFormProps {
  draft: ApplicationDraft;
  onChange: (draft: ApplicationDraft) => void;
  problems: DraftProblems;
  disabled?: boolean;
}

interface TextFieldProps {
  id: keyof ApplicationDraft;
  label: string;
  value: string;
  hint?: string;
  problem?: string;
  disabled?: boolean;
  wide?: boolean;
  onChange: (value: string) => void;
}

function TextField({
  id,
  label,
  value,
  hint,
  problem,
  disabled,
  wide,
  onChange,
}: TextFieldProps) {
  const hintId = hint ? `${id}-hint` : undefined;
  const problemId = problem ? `${id}-problem` : undefined;
  const describedBy = [hintId, problemId].filter(Boolean).join(' ') || undefined;
  return (
    <div className={`field${wide ? ' field--wide' : ''}`}>
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      {/* ALWAYS rendered, empty or not. The row slots below are a CSS subgrid, and a slot
          that disappears when a field has no hint is a slot the neighbouring field cannot
          line up against — which is why "Alcohol content" (two-line hint) and "Net
          contents" (one-line hint) had their inputs at different heights. */}
      <span className="field__hint" id={hintId} aria-hidden={hint ? undefined : true}>
        {hint}
      </span>
      <input
        className="field__input"
        id={id}
        name={id}
        type="text"
        value={value}
        disabled={disabled}
        autoComplete="off"
        spellCheck={false}
        aria-describedby={describedBy}
        aria-invalid={problem ? true : undefined}
        onChange={(event) => onChange(event.target.value)}
      />
      {/* Same reasoning, and it is the one the user asked for by name: when one field in a
          row shows an error and its neighbour does not, both must stay aligned. The slot
          is always here; only its contents change. */}
      <span className="field__problem" id={problemId} aria-hidden={problem ? undefined : true}>
        {problem}
      </span>
    </div>
  );
}

export default function ApplicationForm({
  draft,
  onChange,
  problems,
  disabled = false,
}: ApplicationFormProps) {
  const set = <K extends keyof ApplicationDraft>(key: K, value: ApplicationDraft[K]) =>
    onChange({ ...draft, [key]: value });

  return (
    <fieldset className="form" disabled={disabled}>
      <legend className="form__legend">
        <span className="form__step">Step 2</span>
        What the application says
      </legend>

      <div className="field">
        <label className="field__label" htmlFor="commodity">
          Kind of product
        </label>
        {/* The empty hint slot is load-bearing. `.field` spans four subgrid rows, so a
            field with only two children puts its control in row 2 while every TextField
            beside it puts its input in row 3 — which is why this select sat a whole row
            above "Brand name". The slot costs nothing and keeps the row honest. */}
        <span className="field__hint" aria-hidden="true" />
        <select
          className="field__input field__select"
          id="commodity"
          name="commodity"
          value={draft.commodity}
          onChange={(event) => set('commodity', event.target.value as Commodity)}
        >
          {(Object.keys(COMMODITY_LABELS) as Commodity[]).map((key) => (
            <option key={key} value={key}>
              {COMMODITY_LABELS[key]}
            </option>
          ))}
        </select>
        <span className="field__problem" aria-hidden="true" />
      </div>

      <TextField
        id="brand_name"
        label="Brand name"
        hint="e.g. Old Tom Distillery"
        value={draft.brand_name}
        problem={problems.brand_name}
        onChange={(value) => set('brand_name', value)}
      />

      <TextField
        id="class_type"
        label="Class / type"
        hint="e.g. Kentucky Straight Bourbon Whiskey"
        value={draft.class_type}
        problem={problems.class_type}
        wide
        onChange={(value) => set('class_type', value)}
      />

      <TextField
        id="alcohol_content"
        label="Alcohol content"
        hint="Percent by volume, e.g. 45. Leave empty if the application does not state it."
        value={draft.alcohol_content}
        problem={problems.alcohol_content}
        onChange={(value) => set('alcohol_content', value)}
      />

      <TextField
        id="net_contents"
        label="Net contents"
        hint="e.g. 750 mL"
        value={draft.net_contents}
        problem={problems.net_contents}
        onChange={(value) => set('net_contents', value)}
      />

      <TextField
        id="producer_name"
        label="Producer or bottler"
        hint="e.g. Old Tom Distillery"
        value={draft.producer_name}
        problem={problems.producer_name}
        onChange={(value) => set('producer_name', value)}
      />

      <TextField
        id="producer_address"
        label="Producer city and state"
        hint="e.g. Bardstown, Kentucky"
        value={draft.producer_address}
        problem={problems.producer_address}
        onChange={(value) => set('producer_address', value)}
      />

      <div className="field field--wide">
        <label className="field__checkbox" htmlFor="is_import">
          <input
            id="is_import"
            name="is_import"
            type="checkbox"
            checked={draft.is_import}
            onChange={(event) => set('is_import', event.target.checked)}
          />
          <span>This product is imported</span>
        </label>
      </div>

      {draft.is_import ? (
        <TextField
          id="country_of_origin"
          label="Country of origin"
          value={draft.country_of_origin}
          problem={problems.country_of_origin}
          wide
          onChange={(value) => set('country_of_origin', value)}
        />
      ) : null}
    </fieldset>
  );
}
