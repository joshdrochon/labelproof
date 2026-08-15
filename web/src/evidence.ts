/**
 * Where the outlines on the label picture come from — for every screen that draws them.
 *
 * This lived inside `VerifyNow`'s checklist render until the batch drill-in needed the
 * same picture with the same outlines (HITL-3). It is a shared module rather than a
 * second copy because the numbering is a *promise between two places*: a row that reads
 * "Outlined as 3 on the picture" and the circle labelled 3 are tied together by nothing
 * except this function producing the same order both times. Two builders would agree on
 * the day they were written and drift on the first change to `triage.ts` — and an outline
 * pointing at the wrong words is worse than no outline, because it is trusted.
 *
 * Two rules carried over intact from where this used to live:
 *
 *   1. **No bbox, no region.** A field the extractor gave no area for gets nothing drawn.
 *      Never a guessed one (pinned build decision).
 *   2. **Only attention rows are numbered.** A settled row still produces a region, so
 *      hovering it can light up the words it was read from, but it carries no number:
 *      the numbers are the agent's worklist, and numbering the seven rows that are fine
 *      is how the one that is not gets lost.
 */

import type { EvidenceRegion } from './components/EvidenceOverlay';
import { fieldLabel } from './copy';
import { attentionFields, settledFields } from './triage';
import type { FieldName, FieldResult } from './types';

/** The regions for one result, numbered in the order an agent should work them. */
export function evidenceRegions(fields: FieldResult[]): EvidenceRegion[] {
  let counter = 0;

  const build = (row: FieldResult, needsAttention: boolean): EvidenceRegion[] => {
    const bbox = row.evidence?.bbox;
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

  // Attention rows first, so the counter walks them in triage order and never has to be
  // reconciled afterwards against a list sorted differently.
  return [
    ...attentionFields(fields).flatMap((row) => build(row, true)),
    ...settledFields(fields).flatMap((row) => build(row, false)),
  ];
}

/**
 * The region for one field, or null when nothing was drawn for it.
 *
 * **Every screen must take its row number from here rather than counting rows itself.**
 * A checklist position and a region number are not the same sequence: `build()` above
 * skips any row with no bbox, so one `missing` government warning — which sorts FIRST —
 * makes every later "outlined as N" point at the region before the one it names. The
 * batch drill-in shipped counting positions and did exactly that. This is the whole
 * reason the builder was extracted, so the lookup lives beside it.
 */
export function regionFor(
  regions: EvidenceRegion[],
  field: FieldName | null,
): EvidenceRegion | null {
  if (!field) return null;
  return regions.find((region) => region.field === field) ?? null;
}

/** The number an agent sees against a row, or null when it has no outline. */
export function numberFor(regions: EvidenceRegion[], field: FieldName): number | null {
  return regionFor(regions, field)?.number ?? null;
}
