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

/** Reads the number out of whatever was pasted: `45`, `45.0%`, `45% ABV`, `80 proof`. */
export function parseAlcoholContent(raw: string): number | null {
  const match = raw.match(/-?\d+(\.\d+)?/);
  if (!match) return null;
  const value = Number.parseFloat(match[0]);
  return Number.isFinite(value) ? value : null;
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
  if (
    draft.alcohol_content.trim() !== '' &&
    parseAlcoholContent(draft.alcohol_content) === null
  ) {
    problems.alcohol_content = 'Enter the alcohol content as a number, for example "45".';
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
      {hint ? (
        <span className="field__hint" id={hintId}>
          {hint}
        </span>
      ) : null}
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
      {problem ? (
        <span className="field__problem" id={problemId}>
          {problem}
        </span>
      ) : null}
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
      <legend className="form__legend">What the application says</legend>

      <div className="field">
        <label className="field__label" htmlFor="commodity">
          Kind of product
        </label>
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
      </div>

      <TextField
        id="brand_name"
        label="Brand name"
        value={draft.brand_name}
        problem={problems.brand_name}
        onChange={(value) => set('brand_name', value)}
      />

      <TextField
        id="class_type"
        label="Class / type"
        hint="For example: Kentucky Straight Bourbon Whiskey"
        value={draft.class_type}
        problem={problems.class_type}
        wide
        onChange={(value) => set('class_type', value)}
      />

      <TextField
        id="alcohol_content"
        label="Alcohol content"
        hint="Percent by volume. Leave empty if the application does not state it."
        value={draft.alcohol_content}
        problem={problems.alcohol_content}
        onChange={(value) => set('alcohol_content', value)}
      />

      <TextField
        id="net_contents"
        label="Net contents"
        hint="For example: 750 mL"
        value={draft.net_contents}
        problem={problems.net_contents}
        onChange={(value) => set('net_contents', value)}
      />

      <TextField
        id="producer_name"
        label="Producer or bottler"
        value={draft.producer_name}
        problem={problems.producer_name}
        onChange={(value) => set('producer_name', value)}
      />

      <TextField
        id="producer_address"
        label="Producer city and state"
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
