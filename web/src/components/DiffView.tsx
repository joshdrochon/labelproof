/**
 * "What the application says" against "what the label shows", with the difference marked.
 *
 * Agents do this comparison with their eyes today (Jenny: *"check with my eyes"*), so the
 * job here is to make the eye land on the changed word rather than to hide the comparison
 * behind a verdict. Two rules:
 *
 *   - Marked text is marked with an **underline and a tint**, never a tint alone, so it
 *     survives printing and colour blindness.
 *   - Values render in a monospaced face. Character-level differences — a curly
 *     apostrophe, a doubled space, `733 mL` against `750 mL` — are legible in mono and
 *     easy to miss in a proportional face.
 */

import { useMemo } from 'react';

export type Piece = { text: string; changed: boolean };

/** Longest common subsequence over tokens; everything off the path is a difference. */
function diffTokens(a: string[], b: string[]): [Piece[], Piece[]] {
  const n = a.length;
  const m = b.length;
  const table: number[][] = Array.from({ length: n + 1 }, () =>
    new Array<number>(m + 1).fill(0),
  );
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i]![j] =
        a[i] === b[j]
          ? table[i + 1]![j + 1]! + 1
          : Math.max(table[i + 1]![j]!, table[i]![j + 1]!);
    }
  }
  const left: Piece[] = [];
  const right: Piece[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      left.push({ text: a[i]!, changed: false });
      right.push({ text: b[j]!, changed: false });
      i += 1;
      j += 1;
    } else if (table[i + 1]![j]! >= table[i]![j + 1]!) {
      left.push({ text: a[i]!, changed: true });
      i += 1;
    } else {
      right.push({ text: b[j]!, changed: true });
      j += 1;
    }
  }
  while (i < n) {
    left.push({ text: a[i]!, changed: true });
    i += 1;
  }
  while (j < m) {
    right.push({ text: b[j]!, changed: true });
    j += 1;
  }
  return [left, right];
}

function tokenize(value: string): string[] {
  const words = value.split(/(\s+)/).filter((t) => t !== '');
  // A single short token is compared letter by letter — that is where 45 vs 40 lives.
  if (words.filter((w) => w.trim() !== '').length <= 1 && value.length <= 24) {
    return [...value];
  }
  return words;
}

function collapse(pieces: Piece[]): Piece[] {
  const out: Piece[] = [];
  for (const piece of pieces) {
    const last = out[out.length - 1];
    if (last && last.changed === piece.changed) last.text += piece.text;
    else out.push({ ...piece });
  }
  return out;
}

/** The two marked-up sides. Exposed so a table row can put each side in its own cell. */
export function useValueDiff(
  expected: string | null,
  extracted: string | null,
  mark = true,
): [Piece[], Piece[]] {
  return useMemo(() => {
    const a = expected ?? '';
    const b = extracted ?? '';
    if (!mark || a === '' || b === '' || a === b) {
      return [
        a ? [{ text: a, changed: false }] : [],
        b ? [{ text: b, changed: false }] : [],
      ] as [Piece[], Piece[]];
    }
    const [l, r] = diffTokens(tokenize(a), tokenize(b));
    return [collapse(l), collapse(r)] as [Piece[], Piece[]];
  }, [expected, extracted, mark]);
}

export function DiffText({ pieces, empty }: { pieces: Piece[]; empty: string }) {
  if (pieces.length === 0) return <span className="diff__empty">{empty}</span>;
  return (
    <>
      {pieces.map((piece, index) =>
        piece.changed ? (
          <mark className="diff__changed" key={index}>
            {piece.text}
          </mark>
        ) : (
          <span key={index}>{piece.text}</span>
        ),
      )}
    </>
  );
}

interface DiffViewProps {
  expected: string | null;
  extracted: string | null;
  /** Off for rows that agree — marking every character of an identical pair is noise. */
  mark?: boolean;
}

/**
 * The full side-by-side block. Used for long values — the government warning statement
 * above all — where a table cell cannot show the whole text.
 */
export default function DiffView({ expected, extracted, mark = true }: DiffViewProps) {
  const [left, right] = useValueDiff(expected, extracted, mark);
  return (
    <div className="diff diff--block">
      <div className="diff__side">
        <span className="diff__caption">The application says</span>
        <p className="diff__value" data-testid="diff-expected">
          <DiffText pieces={left} empty="Nothing filed for this field" />
        </p>
      </div>
      <div className="diff__side">
        <span className="diff__caption">The label shows</span>
        <p className="diff__value" data-testid="diff-extracted">
          <DiffText pieces={right} empty="Nothing found on the label" />
        </p>
      </div>
    </div>
  );
}
