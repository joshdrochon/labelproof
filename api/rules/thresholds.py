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

#: Below this on any dimension, the image is hopeless: return Unreadable with a retake
#: reason and make ZERO model calls (LP-321). The pre-gate can only ever spend less and
#: can never produce a false pass, because its outcome is "we did not verify".
HOPELESS: Final[float] = 0.20

#: Below this the image is degraded — worth extracting, but per-field legibility is
#: suspect and confidence should be discounted.
DEGRADED: Final[float] = 0.45

#: Edge-gradient variance in the worse of the two axes, treated as fully sharp. Scoring is
#: LOGARITHMIC between the two bounds below, because the measure spans four orders of
#: magnitude on real content. Measured on the robustness set: a sharp rendered label sits
#: near 10800, a defocus of radius 2 near 2300, radius 6 near 430, radius 12 near 114, and
#: a 25-pixel motion smear near 180.
#:
#: The worse *axis* rather than an isotropic Laplacian, because camera shake destroys one
#: direction and leaves the other intact — the isotropic measure scored a smear nobody
#: could read the same as a photograph that was merely soft.
#:
#: Calibrated against rendered fixtures, not photographs. `scripts/calibrate_quality.py`
#: retunes them against Tier B and reports what each change does to false passes.
SHARP_GRADIENT_VARIANCE: Final[float] = 6000.0

#: At or below this, treated as fully blurred. Set between the radius-12 defocus (114) and
#: the 25-pixel motion smear (178), both of which are past reading.
BLUR_HOPELESS_VARIANCE: Final[float] = 120.0

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
