/**
 * Every word the agent reads.
 *
 * Kept in one file on purpose: the copy pass (LP-115) is a review of this file, not a
 * hunt through twelve components. House rules, all of them from PRD §Usability:
 *
 *   - Agents' vocabulary. "Request a better image", not "unreadable_image".
 *   - No ML jargon. No "extraction", "inference", "confidence threshold", "model".
 *   - The app recommends. Never "Approve this" — always "Recommendation: ...".
 *   - A verdict is a WORD. Colour and icon are decoration on top of the word, never
 *     a substitute for it (UX-3, and Dave prints in black and white).
 */

import type { Commodity, FieldName, Recommendation, Verdict } from './types';

export const FIELD_LABELS: Record<FieldName, string> = {
  brand_name: 'Brand name',
  class_type: 'Class / type',
  alcohol_content: 'Alcohol content',
  net_contents: 'Net contents',
  producer: 'Producer name and address',
  country_of_origin: 'Country of origin',
  government_warning: 'Government warning',
};

export function fieldLabel(field: FieldName): string {
  return FIELD_LABELS[field] ?? field;
}

export interface VerdictMeta {
  /** The word. Always rendered. Never replaced by the icon or the colour. */
  word: string;
  /** Shape id — distinct outline per verdict, so grayscale still separates them. */
  icon: VerdictIcon;
  /** One line, plain language, telling the agent what this row means. */
  meaning: string;
  /** What to do next. Advice to a person, never an instruction to obey. */
  whatToDo: string;
}

export type VerdictIcon =
  | 'check'
  | 'tilde'
  | 'cross'
  | 'empty'
  | 'question'
  | 'dash';

export const VERDICTS: Record<Verdict, VerdictMeta> = {
  match: {
    word: 'Match',
    icon: 'check',
    meaning: 'The label says the same thing as the application.',
    whatToDo: 'Nothing. This row agrees with the application.',
  },
  acceptable_variation: {
    word: 'Acceptable variation',
    icon: 'tilde',
    meaning:
      'The wording differs but appears to mean the same thing. This is a judgment call, so it is shown to you rather than passed quietly.',
    whatToDo:
      'Read both values side by side and decide whether the difference matters.',
  },
  mismatch: {
    word: 'Mismatch',
    icon: 'cross',
    meaning: 'The label and the application say different things.',
    whatToDo:
      'Check the label image yourself. If the label is wrong, this is grounds to return the application for correction.',
  },
  missing: {
    word: 'Missing',
    icon: 'empty',
    meaning: 'This element was required and was not found anywhere on the label.',
    whatToDo:
      'Confirm it is not on a part of the label you have not photographed — the warning is usually on the back. If it truly is not there, return the application for correction.',
  },
  unreadable: {
    word: 'Unreadable',
    icon: 'question',
    meaning:
      'The picture was not clear enough to read this. Nothing has been verified here either way.',
    whatToDo:
      'Request a better image from the applicant, or look at the artwork yourself — the tool could not read it, and a person often can.',
  },
  not_applicable: {
    word: 'Not applicable',
    icon: 'dash',
    meaning: 'This element is not required for this kind of product.',
    whatToDo: 'Nothing. This row does not apply here.',
  },
};

export interface RecommendationMeta {
  /** Shown after the fixed "Recommendation:" prefix. Never phrased as an order. */
  word: string;
  icon: VerdictIcon;
  tone: 'clear' | 'attention' | 'serious';
}

/**
 * Three, and always prefixed. The prefix is not decoration — it is the difference
 * between advice and an instruction, and the agent makes the determination (HITL-1).
 */
export const RECOMMENDATION_PREFIX = 'Recommendation:';

export const RECOMMENDATIONS: Record<Recommendation, RecommendationMeta> = {
  ready_to_approve: {
    word: 'Ready to approve',
    icon: 'check',
    tone: 'clear',
  },
  needs_review: {
    word: 'Needs review',
    icon: 'tilde',
    tone: 'attention',
  },
  return_for_correction: {
    word: 'Return for correction',
    icon: 'cross',
    tone: 'serious',
  },
};

/**
 * Reference citations, copied from `api/canon.py`'s CITATIONS table.
 *
 * Shown as background reading beside a row, clearly labelled "Reference". A citation
 * that belongs to an actual finding comes from the server on that finding and is
 * rendered there instead. Nothing is cited here that canon.py does not state, so no
 * row ever displays a regulation this project has not verified.
 */
export function referenceCitations(
  field: FieldName,
  commodity: Commodity,
): string[] {
  switch (field) {
    case 'government_warning':
      return ['27 CFR 16.21', '27 CFR 16.22'];
    case 'alcohol_content':
      if (commodity === 'spirits') return ['27 CFR 5.65'];
      if (commodity === 'wine') return ['27 CFR 4.36'];
      return ['27 CFR 7.65'];
    case 'net_contents':
      if (commodity === 'spirits') return ['27 CFR 5.203'];
      if (commodity === 'wine') return ['27 CFR 4.72'];
      return [];
    default:
      return [];
  }
}

export const COMMODITY_LABELS: Record<Commodity, string> = {
  spirits: 'Distilled spirits',
  wine: 'Wine',
  malt: 'Malt beverage',
};

/**
 * Stage narration for the wait. These are the real pipeline stages in the real order
 * (pinned build decision), so the sentence on screen is a description of what is happening and
 * not a decorative animation.
 */
export const STAGES: string[] = [
  'Checking the picture is clear enough to read…',
  'Reading the label…',
  'Comparing the label with the application…',
  'Checking the government warning word for word…',
  'Putting the checklist together…',
];

/** Plain-language fallbacks when the server sends an error with no message. */
export const ERROR_FALLBACK: Record<string, string> = {
  user: 'Something in what was sent could not be used.',
  image: 'That picture could not be read.',
  provider: 'The checking service is not answering right now. Nothing was verified.',
  internal: 'Something went wrong on our side. Nothing was verified.',
};

export const NEXT_STEP_LABELS: Record<string, string> = {
  retake: 'Request a better image',
  retry: 'Try again',
  fix_input: 'Check the application details',
};
