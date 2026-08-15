/**
 * The evidence panel.
 *
 * The rule under test is the safety one: **no box, no highlight.** A guessed region is a
 * false trust signal in a product whose whole argument is honest evidence, so a field
 * with no bbox must draw nothing at all. Tag collision is tested too — overlapping tags
 * were a named defect in the mock review.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import EvidenceOverlay, { placeTags } from './EvidenceOverlay';
import type { EvidenceRegion } from './EvidenceOverlay';

function region(
  field: EvidenceRegion['field'],
  bbox: EvidenceRegion['bbox'],
  overrides: Partial<EvidenceRegion> = {},
): EvidenceRegion {
  return {
    field,
    label: field,
    bbox,
    imageIndex: 0,
    number: 1,
    needsAttention: true,
    ...overrides,
  };
}

describe('evidence overlay', () => {
  it('draws a region for a field that has one', () => {
    render(
      <EvidenceOverlay
        imageUrl="/sample/images/front.png"
        imageIndex={0}
        imageLabel="Label picture — front"
        regions={[region('government_warning', { x0: 0.1, y0: 0.7, x1: 0.9, y1: 0.9 })]}
        activeField={null}
      />,
    );
    expect(screen.getByTestId('region-government_warning')).toBeInTheDocument();
  });

  it('draws nothing for a field with no region — never a guess', () => {
    // A field with no bbox never becomes an EvidenceRegion in the first place, so the
    // panel has nothing to draw and says nothing about where the value came from.
    render(
      <EvidenceOverlay
        imageUrl="/sample/images/front.png"
        imageIndex={0}
        imageLabel="Label picture — front"
        regions={[]}
        activeField="brand_name"
      />,
    );
    expect(screen.queryByTestId('region-brand_name')).not.toBeInTheDocument();
    expect(document.querySelectorAll('.evidence__box')).toHaveLength(0);
  });

  it('keeps quiet regions off the picture until their row is active', () => {
    const regions = [
      region('brand_name', { x0: 0.1, y0: 0.1, x1: 0.5, y1: 0.2 }, {
        needsAttention: false,
        number: null,
      }),
    ];
    const { rerender } = render(
      <EvidenceOverlay
        imageUrl="/x.png"
        imageIndex={0}
        imageLabel="Label"
        regions={regions}
        activeField={null}
      />,
    );
    expect(screen.queryByTestId('region-brand_name')).not.toBeInTheDocument();

    rerender(
      <EvidenceOverlay
        imageUrl="/x.png"
        imageIndex={0}
        imageLabel="Label"
        regions={regions}
        activeField="brand_name"
      />,
    );
    expect(screen.getByTestId('region-brand_name')).toBeInTheDocument();
  });

  it('never lets two tags sit on top of each other', () => {
    const placed = placeTags([
      region('brand_name', { x0: 0.1, y0: 0.3, x1: 0.6, y1: 0.36 }),
      region('class_type', { x0: 0.12, y0: 0.305, x1: 0.6, y1: 0.36 }),
      region('net_contents', { x0: 0.11, y0: 0.31, x1: 0.6, y1: 0.36 }),
    ]);
    for (let i = 0; i < placed.length; i += 1) {
      for (let j = i + 1; j < placed.length; j += 1) {
        const a = placed[i]!;
        const b = placed[j]!;
        const overlapping =
          Math.abs(a.top - b.top) < 7.5 && Math.abs(a.left - b.left) < 16;
        expect(overlapping).toBe(false);
      }
    }
  });

  it('leaves distant tags exactly where their region is', () => {
    const placed = placeTags([
      region('brand_name', { x0: 0.1, y0: 0.1, x1: 0.5, y1: 0.2 }),
      region('government_warning', { x0: 0.1, y0: 0.7, x1: 0.9, y1: 0.9 }),
    ]);
    expect(placed.map((tag) => Math.round(tag.top))).toEqual([10, 70]);
  });

  it('says out loud when the outlines are only approximate', () => {
    render(
      <EvidenceOverlay
        imageUrl="/x.png"
        imageIndex={0}
        imageLabel="Label"
        regions={[region('brand_name', { x0: 0.1, y0: 0.1, x1: 0.5, y1: 0.2 })]}
        activeField={null}
        geometryIsApproximate
      />,
    );
    expect(screen.getByText(/can sit a little off/i)).toBeInTheDocument();
  });
});


describe('tags stay inside the picture', () => {
  const region = (field: string, x0: number, y0: number) => ({
    field,
    label: field,
    number: 1,
    needsAttention: false,
    bbox: { x0, y0, x1: x0 + 0.1, y1: y0 + 0.1 },
  });

  it('anchors a tag near the right edge by its right side', () => {
    // `left` positions the START of a tag, and a tag is as wide as its words. Left-anchored
    // at the far edge, "Government warning" ran off the image and over the checklist.
    const [tag] = placeTags([region('government_warning', 0.78, 0.05) as never]);
    expect(tag.flipped).toBe(true);
  });

  it('leaves a tag on the left side anchored normally', () => {
    const [tag] = placeTags([region('brand_name', 0.1, 0.05) as never]);
    expect(tag.flipped).toBe(false);
  });

  it('separates two wide tags that sit 18% apart on the same line', () => {
    // The real case: a landscape label with the warning up the right edge and the
    // producer beside it. 18% apart is outside the old 16% window, so nothing moved them,
    // and they overlapped on screen because neither tag is 16% of anything.
    const placed = placeTags([
      region('government_warning', 0.78, 0.05) as never,
      region('producer', 0.6, 0.05) as never,
    ]);
    const [a, b] = placed;
    expect(Math.abs(a.top - b.top)).toBeGreaterThanOrEqual(7.5);
  });
});
