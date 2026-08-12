"""Every tunable threshold, in one place (LP-327).

Named constants, never inline magic numbers scattered across modules. Two reasons:

1. **Tightening must be cheap.** The flag posture starts balanced and moves up if the eval
   shows things slipping through — that has to be a one-line change with evidence behind
   it, not a hunt through five files.
2. **The eval sweeps these.** `eval/run.py --sweep-thresholds` reports false passes at
   each level, so a change is justified by a number rather than a feeling.

**The government warning is exempt from all of it.** No threshold here relaxes the warning
statement — it fails closed unconditionally (PRD §Constraints, WARN-6). If you find
yourself adding a warning-related knob, that is the bug.

Values below are starting points. LP-200 and LP-292 replace them with measured ones.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------------------
# Confidence routing (MATCH-6)
# --------------------------------------------------------------------------------------

#: Below this, a non-warning field routes to Needs review rather than reporting a verdict.
#:
#: NOT YET WIRED for ordinary fields — LP-224 is open and this constant has no call site
#: on that path. Said here rather than left to be discovered, because a threshold that
#: exists only as a number reads exactly like one that is enforced.
CONFIDENCE_FLOOR: Final[float] = 0.75

#: Below this, a gray case escalates to Tier-3 adjudication rather than falling through
#: to Mismatch.
TIER3_TRIGGER: Final[float] = 0.90

#: Below this, escalate to a second extraction pass cropped to the field's region
#: (LP-325). Costs a call; only fires where the first pass was unsure.
ESCALATION_TRIGGER: Final[float] = 0.60


# --------------------------------------------------------------------------------------
# Image quality (IMG-4)
# --------------------------------------------------------------------------------------
#
# All scores are 0..1 where 1 is best, so they compose and read consistently.
#
# **How these were chosen (LP-200).** `python -m scripts.calibrate_quality` sweeps each
# value below across a range and reports, at every level, false passes (something
# illegible treated as legible) and false flags (something legible treated as illegible).
# The rule it exists to enforce: *a threshold change that reduces flags by letting a bad
# label through is a regression, not an improvement.*
#
# Recorded from the run against the 19-condition robustness set. "Margin" is how many
# sweep steps the value sits from the nearest level that produces a false pass:
#
#     HOPELESS                   0.20    clean band 0.10–0.25    margin 3
#     DEGRADED                   0.45    clean band 0.30–0.70    margin 8 (none in range)
#     SHARP_GRADIENT_VARIANCE    1200    clean band 600–1600     margin 6 (none in range)
#     BLUR_HOPELESS_VARIANCE      110    clean band 60–110       margin 3
#     EXPOSURE_FLOOR               90    clean band 50–160       margin 6 (none in range)
#     GLARE_SATURATION_FRACTION  0.25    clean band 0.10–0.35    margin 2
#     MIN_LONG_EDGE_PX           1200    clean band 600–2000     margin 5 (none in range)
#
# **How much weaker this is than it looks, specifically.** "Calibrated against rendered
# fixtures" is too vague to act on, so here is what is actually missing:
#
# 1. *The false-pass column is computed over 9 of the 19 conditions.* A condition whose
#    obligation is `readable` has nothing illegible to miss, so it can only ever produce a
#    false flag. Only the eight `pregated` cases and the one `warning_illegible` case can
#    move the column that decides these values.
# 2. *Rendered labels have no sensor noise*, and noise is what broke the previous blur
#    measure — it inflated an unreadable photo tenfold and put it through the gate. There
#    is now one noisy fixture, built by adding Gaussian noise, which is not the same thing
#    as a real sensor at high ISO.
# 3. *A band being wide is not the same as a threshold being robust.* DEGRADED, EXPOSURE
#    FLOOR and MIN_LONG_EDGE_PX show no false pass anywhere in range, which means this set
#    does not exercise them, not that they are safe.
#
# Re-run the sweep with `--photos` when Tier B lands; that run decides these values
# (LP-292).

#: Below this on any dimension, the image is hopeless: return Unreadable with a retake
#: reason and make ZERO model calls (LP-321). The pre-gate can only ever spend less and
#: can never produce a false pass, because its outcome is "we did not verify".
HOPELESS: Final[float] = 0.20

#: Below this the image is degraded — worth extracting, but per-field legibility is
#: suspect and confidence should be discounted.
DEGRADED: Final[float] = 0.45

#: Directional edge-gradient variance, minimised over eight orientations and measured
#: after a σ=2 pre-smooth, treated as fully sharp. Scoring is LOGARITHMIC between the two
#: bounds below because the measure spans two and a half decades on real content.
#:
#: Measured on the robustness set at this operator: clean 1255, defocus radius 2 at 926,
#: radius 6 at 297, radius 12 at 87, radius 16 at 42. A 51-pixel motion smear lands
#: between 24 and 58 depending on angle, and a 25-pixel smear between 77 and 147.
#:
#: Set to 1200 rather than 1255 so a *dark but sharp* label still scores a full 1.0 — it
#: measures 1236, and the whole point of normalising contrast away first is that "too
#: dark" and "too blurry" stay separate problems.
SHARP_GRADIENT_VARIANCE: Final[float] = 1200.0

#: At or below this, treated as fully blurred. Sits above the radius-12 defocus (87) and
#: above every 51-pixel motion smear at every angle (24–58), both of which are past
#: reading, and below the radius-6 defocus (297) which is merely degraded.
BLUR_HOPELESS_VARIANCE: Final[float] = 110.0

#: Fraction of pixels at or near saturation before glare is considered total.
GLARE_SATURATION_FRACTION: Final[float] = 0.25

#: Mean luminance at or above which an image is considered well lit, on a 0..255 scale.
#: There is deliberately no ceiling — labels are mostly light, so high mean luminance is
#: normal. Blown-out highlights are counted by glare_score instead; penalising brightness
#: here as well would double-count and would score a cream label as defective.
EXPOSURE_FLOOR: Final[float] = 90.0

#: Skew beyond this many degrees is corrected before extraction rather than tolerated.
SKEW_CORRECTION_DEG: Final[float] = 1.5

#: Long edge below this loses the high-resolution vision tier, so small text — the
#: warning statement above all — may not be verifiable. Reported as a finding, never
#: used to route to a cheaper model (see JUDGMENT-LOG).
MIN_LONG_EDGE_PX: Final[int] = 1200
