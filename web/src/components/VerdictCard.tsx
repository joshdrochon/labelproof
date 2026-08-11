/**
 * The verdict chip — icon + word, never colour alone.
 *
 * This is the single most load-bearing component in the app, so it is also the most
 * constrained:
 *
 *   - The **word** is always rendered as text. It is never replaced by the icon, never
 *     replaced by the colour, and never truncated. Take the stylesheet away entirely
 *     and the screen still reads correctly.
 *   - Each verdict gets a **structurally different shape**, not a different hue of the
 *     same shape. Photocopy it, print it on Dave's black-and-white printer, or look at
 *     it with deuteranopia and the six chips are still six different things.
 *   - Colour is the third channel, added last, and carries nothing on its own.
 */

import type { Verdict } from '../types';
import type { VerdictIcon } from '../copy';
import { VERDICTS } from '../copy';

interface GlyphProps {
  icon: VerdictIcon;
}

/** Distinct outlines. Deliberately not six variations of a circle. */
export function VerdictGlyph({ icon }: GlyphProps) {
  const common = {
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 2.4,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    className: 'glyph',
    'aria-hidden': true,
    focusable: false,
    'data-icon': icon,
  };

  switch (icon) {
    case 'check':
      return (
        <svg {...common}>
          <path d="M4 12.5 L9.5 18 L20 6" />
        </svg>
      );
    case 'tilde':
      return (
        <svg {...common}>
          <path d="M3 9c2.4-3.4 5.2-3.4 7.6 0s5.2 3.4 7.6 0" />
          <path d="M3 16.5c2.4-3.4 5.2-3.4 7.6 0s5.2 3.4 7.6 0" />
        </svg>
      );
    case 'cross':
      return (
        <svg {...common}>
          <path d="M5 5 L19 19" />
          <path d="M19 5 L5 19" />
        </svg>
      );
    case 'empty':
      return (
        <svg {...common}>
          <rect x="3.5" y="3.5" width="17" height="17" rx="2" strokeDasharray="4 3" />
        </svg>
      );
    case 'question':
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" />
          <path d="M9.2 9.2a2.9 2.9 0 1 1 3.6 2.9c-.7.2-1 .8-1 1.5v.6" />
          <path d="M11.8 17.4h.01" strokeWidth={2.8} />
        </svg>
      );
    case 'dash':
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" />
          <path d="M7.5 12 L16.5 12" />
        </svg>
      );
  }
}

interface VerdictChipProps {
  verdict: Verdict;
  /** Attention rows get the large chip; settled rows get the quiet one. */
  size?: 'lg' | 'sm';
}

export default function VerdictChip({ verdict, size = 'sm' }: VerdictChipProps) {
  const meta = VERDICTS[verdict];
  return (
    <span
      className={`chip chip--${size}`}
      data-verdict={verdict}
      data-testid={`verdict-chip-${verdict}`}
    >
      <VerdictGlyph icon={meta.icon} />
      <span className="chip__word">{meta.word}</span>
    </span>
  );
}
