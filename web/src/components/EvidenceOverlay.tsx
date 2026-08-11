/**
 * The label picture with the evidence regions drawn on it.
 *
 * Four rules, three of them learned from the mock review:
 *
 *   1. **This panel is a viewer, never a dropzone.** Upload and evidence are two states
 *      of one panel and the screen is only ever in one of them. A panel that is both is
 *      a panel where you can drop a file onto the evidence you are reading.
 *   2. **A region is drawn once.** The numbered outline on the photo *is* the evidence;
 *      the same crop repeated beside the row taught the agent nothing and doubled the
 *      reading.
 *   3. **No box, no highlight.** A field with no bbox gets nothing drawn — never a
 *      guessed region. A wrong highlight is worse than none, because the whole argument
 *      of this product is that its evidence is honest (BUILD.md §6).
 *   4. **Tags never sit on top of each other.** Two regions with close top edges would
 *      stack their number tags into an unreadable smear, so tags are pushed apart in a
 *      deterministic pass before render.
 *
 * Known contract gap, stated rather than papered over: boxes are measured against the
 * *preprocessed* image, and the server does not yet return a URL for it. Until it does,
 * this draws over the local upload, which is the same picture before deskew. The caller
 * passes `geometryIsApproximate` when that is what is happening, and the panel says so
 * out loud instead of implying pixel accuracy it does not have.
 */

import { useMemo } from 'react';
import type { BoundingBox, FieldName } from '../types';

export interface EvidenceRegion {
  field: FieldName;
  label: string;
  bbox: BoundingBox;
  imageIndex: number;
  /** 1-based, and only on rows that need the agent's eyes. */
  number: number | null;
  needsAttention: boolean;
}

interface EvidenceOverlayProps {
  imageUrl: string | null;
  imageIndex: number;
  imageLabel: string;
  regions: EvidenceRegion[];
  activeField: FieldName | null;
  onActivateField?: (field: FieldName | null) => void;
  geometryIsApproximate?: boolean;
  /** Rendered under the picture when the image itself was hard to read. */
  qualityNote?: string | null;
  children?: React.ReactNode;
}

interface PlacedTag {
  region: EvidenceRegion;
  left: number;
  top: number;
}

const TAG_MIN_GAP = 7.5; // percent of image height
const TAG_X_PROXIMITY = 16; // percent of image width

/**
 * Push overlapping tags apart, top to bottom. Deterministic: same input, same layout,
 * which matters because a tag that moves between renders reads as a glitch.
 */
export function placeTags(regions: EvidenceRegion[]): PlacedTag[] {
  const wanted = regions
    .map((region) => ({
      region,
      left: clamp(region.bbox.x0 * 100, 0, 88),
      top: clamp(region.bbox.y0 * 100, 0, 94),
    }))
    .sort((a, b) => a.top - b.top || a.left - b.left);

  const placed: PlacedTag[] = [];
  for (const tag of wanted) {
    let top = tag.top;
    let moved = true;
    let guard = 0;
    while (moved && guard < 32) {
      moved = false;
      guard += 1;
      for (const other of placed) {
        const collides =
          Math.abs(other.top - top) < TAG_MIN_GAP &&
          Math.abs(other.left - tag.left) < TAG_X_PROXIMITY;
        if (collides) {
          top = other.top + TAG_MIN_GAP;
          moved = true;
        }
      }
    }
    placed.push({ region: tag.region, left: tag.left, top: clamp(top, 0, 96) });
  }
  return placed;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export default function EvidenceOverlay({
  imageUrl,
  imageIndex,
  imageLabel,
  regions,
  activeField,
  onActivateField,
  geometryIsApproximate = false,
  qualityNote = null,
  children,
}: EvidenceOverlayProps) {
  const visible = useMemo(
    () =>
      regions.filter(
        (r) =>
          r.imageIndex === imageIndex &&
          (r.needsAttention || r.field === activeField),
      ),
    [regions, imageIndex, activeField],
  );
  const tags = useMemo(() => placeTags(visible), [visible]);

  return (
    <figure className="evidence">
      <figcaption className="evidence__caption">
        <span className="evidence__title">{imageLabel}</span>
        {visible.length > 0 ? (
          <span className="evidence__hint">
            Outlined areas are where each checked value was read.
          </span>
        ) : null}
      </figcaption>

      <div className="evidence__frame">
        {imageUrl ? (
          <img className="evidence__image" src={imageUrl} alt={imageLabel} />
        ) : (
          <p className="evidence__missing">
            The label picture is not available to display. The checklist beside this
            panel is unaffected.
          </p>
        )}

        {imageUrl
          ? tags.map(({ region, left, top }) => {
              const active = region.field === activeField;
              const width = (region.bbox.x1 - region.bbox.x0) * 100;
              const height = (region.bbox.y1 - region.bbox.y0) * 100;
              return (
                <div key={region.field} className="evidence__region-group">
                  <div
                    className="evidence__box"
                    data-active={active ? 'true' : 'false'}
                    data-attention={region.needsAttention ? 'true' : 'false'}
                    data-testid={`region-${region.field}`}
                    style={{
                      left: `${region.bbox.x0 * 100}%`,
                      top: `${region.bbox.y0 * 100}%`,
                      width: `${Math.max(width, 1.5)}%`,
                      height: `${Math.max(height, 1.5)}%`,
                    }}
                  />
                  <button
                    type="button"
                    className="evidence__tag"
                    data-active={active ? 'true' : 'false'}
                    data-attention={region.needsAttention ? 'true' : 'false'}
                    style={{ left: `${left}%`, top: `${top}%` }}
                    onMouseEnter={() => onActivateField?.(region.field)}
                    onMouseLeave={() => onActivateField?.(null)}
                    onFocus={() => onActivateField?.(region.field)}
                    onBlur={() => onActivateField?.(null)}
                    onClick={() => onActivateField?.(region.field)}
                  >
                    {region.number !== null ? (
                      <span className="evidence__tag-number">{region.number}</span>
                    ) : null}
                    <span className="evidence__tag-label">{region.label}</span>
                  </button>
                </div>
              );
            })
          : null}
      </div>

      {qualityNote ? <p className="evidence__note">{qualityNote}</p> : null}
      {geometryIsApproximate && visible.length > 0 ? (
        <p className="evidence__note evidence__note--soft">
          Outlines are placed on the picture you supplied and can sit a little off if the
          photo was taken at an angle. Read the row, then check the area around the
          outline.
        </p>
      ) : null}
      {children}
    </figure>
  );
}
