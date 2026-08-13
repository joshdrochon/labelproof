# Style guide

Every visual decision this interface is allowed to make, as tokens. **`tests/test_style_guide.py`
enforces it** — a value off the scale, a raw hex outside `:root`, or a font nobody serves
fails the build. That gate exists because this file drifted three times before it existed:
`--step` was declared and never referenced while 23 ad-hoc spacing values did the work,
eleven colours lived as literals scattered through the stylesheet, and the font stack named
two faces the app never shipped.

A style guide nothing checks is a description of the past.

---

## Colour

Roles, not names. Nothing is "the blue" — it is the ink, or the rule, or the serious tone.
Every value below is defined once, in `:root` in `web/src/styles.css`, and referenced by
`var()` everywhere else.

| Token | Value | What it is for |
|---|---|---|
| `--paper` | `#f2f0ea` | The page ground |
| `--surface` | `#ffffff` | Cards, panels, table rows that need attention |
| `--surface-sunk` | `#eeece4` | Settled rows — quiet, and 1.18:1 against `--surface` so it is actually visible |
| `--surface-selected` | `#eef1f7` | The row under the cursor or the keyboard |
| `--surface-band` | `#ece7dc` | Group headings inside the checklist |
| `--ink` | `#1b1b19` | Body text |
| `--ink-soft` | `#55514a` | Secondary text. 7.49:1 on `--surface-sunk` |
| `--ink-faint` | `#6b665d` | The quietest ink that still passes AA. **On paper grounds only** — it fails on navy |
| `--rule` | `#948d80` | Row separators. 3.29:1, because a separator in a checklist is structure, not decoration (WCAG 1.4.11) |
| `--rule-strong` | `#6b6558` | Borders that need to read as edges. 5.79:1 |
| `--navy` | `#14213d` | Primary actions, the masthead, headings that lead |
| `--navy-soft` | `#23365e` | Navy on hover |
| `--on-navy` | `#f5f3ee` | Text on a navy ground |
| `--on-navy-soft` | `#cdd4e0` | Secondary text on navy. 10.72:1 |
| `--gold` / `--gold-soft` | `#c0a24a` / `#e9d9a6` | The masthead seal and its rule |
| `--clear` | `#1c5133` | Match |
| `--attention` | `#7a4a00` | Acceptable variation, unreadable |
| `--serious` | `#8a1c11` | Mismatch, missing |
| `--slate` | `#33415c` | Evidence outlines |
| `--mark` | `#f6e7c8` | The diff highlight |
| `--focus` | `#1a4480` | The focus ring. Never removed, never restyled per-component |
| `--on-dark` | `#ffffff` | Text and edges on any dark ground |

**Colour is the third channel.** Every verdict carries a word and a distinct outline before
any colour is applied. Desaturate the whole stylesheet and nothing is lost — `VerdictCard.tsx`
draws six structurally different marks, and the print block flattens every token to black.

**Contrast floors.** 4.5:1 for text, 3:1 for anything non-text that carries meaning. All
21 ink/ground pairs are computed as data in `tests/test_security.py`, not spot-checked.

## Spacing

An 8px scale, the same one USWDS uses. Four tokens; nothing between them.

| Token | Value |
|---|---|
| `--s-1` | `0.5rem` (8px) |
| `--s-2` | `1rem` (16px) |
| `--s-3` | `1.5rem` (24px) — also `--gutter` |
| `--s-4` | `2rem` (32px) |
| `--gutter` | `1.5rem` — the page and card gutter |
| `--radius` | `3px` — one corner radius for the whole interface. A federal form is plainly ruled |

`0.25rem` (4px) is permitted for optical nudges inside a component — the gap between a
label and its input, a badge's inner padding. Everything larger must land on the scale.

## Type

| | |
|---|---|
| `--font-text` | `system-ui` and its fallbacks. Body, labels, controls, **all figures** |
| `--font-display` | `Georgia` first, then Iowan and Charter where they exist. Headings only |
| `--font-mono` | `ui-monospace` and friends. Values shown for comparison |

**We serve no fonts and we name no font we do not serve.** The stack used to lead with
Public Sans — the USWDS face — which does not ship with this app and is not installed by
default on Windows or macOS, so the app never once rendered in it. Nothing in the PRD asked
for it. Naming a font you do not serve is a claim, not a choice.

**Figures come from `--font-text`, always.** Georgia's figures are old-style: 3, 4, 5, 7
and 9 descend below the baseline while 1 and 2 sit on it. In a numbered circle that reads
as the 3 being off-centre, and in a numeric column it reads as mixed heights. Anything
showing a number sets `font-variant-numeric: lining-nums tabular-nums`.

### Sizes

| Value | Used for |
|---|---|
| `1rem` (16px) | **The floor.** Secondary text, captions, table headings |
| `1.0625rem` (17px) | Body, inputs, buttons |
| `1.25rem` (20px) | Attention row labels, card titles |
| `1.5rem` (24px) | Section headings |
| `1.875rem` (30px) | Screen titles |
| `2.25rem` (36px) | The verdict word — the largest thing on the page, because it is the most important |

**16px is the floor and it has no exceptions.** The users include agents over fifty reading
4-point warning text off a bottle all day. Quiet is made with weight, colour and spacing —
never by shrinking type under someone.

### Line height

`1.15` headings · `1.4` the checklist · `1.55` body prose.

### Measure

`66ex` for prose, `72ex` maximum for anything continuous. Two blocks once ran past 150
characters a line, at which point the eye returns to the wrong line and re-reads it.

## Targets

**44px minimum for anything a person clicks** (UX-3). Where a control has to stay visually
small — the numbered chips over a photograph, which would cover the region they point at —
an invisible `::after` carries the hit area. The gate counts the target, not the drawn box.

State it in CSS even when the content already makes it true: a target that is only tall
because of the words inside it stops being tall when the words change.

## Layout

| | |
|---|---|
| Container | `84rem`, on `.main` and the masthead. One container, no nesting |
| Gutter | `--gutter` (24px) |
| Form rows | CSS subgrid — label, hint, input, error are shared rows, so both columns align even when only one field has an error |
| Reflow | Table becomes stacked cards below 900px, which is also a 1280px screen at 200% zoom |

Accent borders are `box-shadow: inset`, never `border-left`. With `box-sizing: border-box`
a decorative border eats its own padding, which once left five stacked cards with five
different left edges spanning 18.6px.

## Voice

Write the way an agent talks. Short sentences, their vocabulary, no jargon.

- **Banned in UI copy:** "inference", "payload", "confidence score", "the model", "tier",
  "adjudication". `tests/test_style_guide.py` greps for them.
- Say what happened and what to do about it. "Could not be read" and "Not found on the
  label" are different sentences because they are different findings.
- Never a number where a phrase will do. `0.87` means nothing to a reader; "this part of
  the label was a little hard to read" means something.
- Keyboard hints name keys that spell the words on the screen — `a` for agree, not `c` for
  a "confirm" button nobody can see.

---

## Changing a token

1. Change it in `:root`.
2. Run `python -m pytest tests/test_style_guide.py tests/test_security.py`.
3. If a contrast or floor gate fails, the token is wrong — not the gate.

Adding a *new* token means adding a row above. The test asserts this file and `:root` agree
in both directions, so a token with no row here fails, and a row here naming a token that
does not exist fails too.
