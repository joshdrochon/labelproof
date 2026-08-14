# Real label photographs — the calibration set the project did not have

23 label images, none of them generated. They arrived late and they immediately found
three things no synthetic fixture could, because every one is a case where the generator
and reality differ in a detail neither the code nor the fixtures had a reason to consider.

## What they found

| | |
|---|---|
| **Glare refused two readable labels** | `glare_score` counts near-saturated pixels. The generator paints paper at ~245 against `BLOWN_LEVEL` 250, so a synthetic label scores a perfect zero. Real scans clip to 255 across ordinary white stock. Two white labels scored glare **exactly 0.000** with blur **1.000** — sharp, evenly lit, refused unread. One was the only label here with no government warning at all, so the most serious defect available went unexamined because the paper was white. Fixed: LP-338 |
| **A compliant producer came back Mismatch** | Two punctuation defects stacked. Token containment kept the comma on `distilling,`; `expand_state_abbreviations` needs a state code followed by a comma or end-of-string, and the label prints `PORTLAND, ME.` with a full stop, so `me.` stayed unexpanded while the application's `ME` became `maine`. Fixed |
| **Prominence over-flags real labels** | **Open.** See below |

## The prominence distribution

`relative_size` is the warning's character height over ordinary body text.
`PROMINENCE_CONCERN_RATIO` is 0.80. Measured across this set:

```
0.4   0.5  0.5  │  0.6  0.6  0.6  0.6   0.7  0.7   0.85 0.85 0.85 0.85   0.9  0.9
    buried      │                        compliant
        ↑ ~0.55 separates                          ↑ 0.80 — current, flags 9 of 15
```

The three at or below 0.5 are the ones confirmed by eye as genuinely buried: a warning
rotated 90° in tiny type, and two set noticeably smaller than the surrounding copy.
Everything from 0.6 up is a normal, compliant label whose warning is simply smaller than
its brand name — which is true of every label ever printed.

The threshold has not been moved. It was calibrated by a sweep that reports false passes
and false flags at each level, and re-fitting it by hand against 23 images would replace
one unrepresentative calibration with another. The number to change is in
`api/rules/typography.py`; the evidence to change it with is here.

**Every one of the 23 also scored `degraded` on whole-image quality. None reached `ok`.**

## What these are not

Not a golden set. There is no application data for any of them, so the field-vs-application
comparison cannot be scored — what is checkable here is everything a label must satisfy on
its own: the warning's wording and typography, standards of fill, the proof cross-check.

They are also not verified ground truth. The list that came with them disagreed with the
images in at least three places, which is the same lesson as `golden/REVIEWED.md`: an
answer key nobody checked can make a run look right or wrong for the wrong reason. Where
this file states what a label is, that is from reading the image.
