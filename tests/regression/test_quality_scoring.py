"""DEFECTS: two image-quality scores that scored the wrong thing.

**Defect one — the exposure ceiling made dimming an image better.** `exposure_score`
treated a mean luminance above 225 as overexposed. Labels are mostly light — cream,
white, foil — so a perfectly good label scored badly, and, worse, the function was
*non-monotonic*: darkening a bright image moved it toward the ideal and raised its
score. An agent could have been told to retake a photograph in worse light. Blown-out
highlights are real, but they are glare, and `glare_score` already counts them; the
ceiling was double-counting.

**Defect two — a linear blur scale collapsed every degree of blur into zero.** Laplacian
variance spans four orders of magnitude on real content: a sharp rendered label sits
near 1400, the same label at Gaussian radius 2 near 37, at radius 12 below 1. Divided
linearly by the sharp value, radius 2 and radius 12 both score ~0.0 — "slightly soft"
and "illegible" become the same answer, and the retake reason an agent reads is the
same for both.

Both are pinned as *shape* properties rather than as remembered numbers. Asserting
`exposure_score(x) == 0.83` would pass on a rewrite that reintroduced the ceiling with
different constants. Asserting monotonicity and separation cannot.

**The blur measure was rewritten after this file was written, and these tests moved with
it rather than being deleted.** `blur_score` no longer takes the Laplacian variance of a
contrast-stretched greyscale. It takes the *worst of eight directional gradient
variances* after a σ=2 pre-smooth, because a Laplacian sums every direction at once and
was blind to motion blur — a 51-pixel smear at 45° scored as merely soft and reached the
model — and because broadband sensor noise inflated the old number tenfold, straight
through the pre-gate. `SHARP_LAPLACIAN_VARIANCE` is gone; `SHARP_GRADIENT_VARIANCE` and
`BLUR_HOPELESS_VARIANCE` are its recalibrated replacements.

What the tests below defend is unchanged, and it is not the operator: it is that the
scale is **logarithmic between the two bounds**, so a ratio of variances maps to a
difference of scores, and that degrees of blur therefore land in **different advice
buckets** instead of collapsing into one. Both still hold. One consequence did change and
is stated where it matters: the pre-smooth compresses the measure's range from roughly
four decades to one, so a *linear* normalization of the new measure would no longer
collapse soft and illegible together. It would still be the wrong scale, and the test
that shows that now says so by the tolerance it actually misses rather than by a
collapse that no longer happens.
"""

from __future__ import annotations

import itertools

import cv2
import numpy as np
import pytest

from api.pipeline import quality
from api.rules import thresholds
from fixtures.generator import degrade
from fixtures.generator.catalog import by_name
from fixtures.generator.render import render

pytestmark = pytest.mark.regression


def _label(brightness: int | None = None) -> np.ndarray:
    """The real rendered Old Tom label — the content these scores are calibrated on.

    Deliberately not a synthetic bar pattern. A hard-edged rectangle grid produces
    ringing at large Gaussian radii, which pushes Laplacian variance back *up* and
    makes blur look non-monotonic. That is an artefact of the test image, and chasing
    it would either weaken the assertion or, worse, ratify a real regression as
    "expected on synthetic input". `thresholds.py` says the calibration is against
    rendered fixtures; so is this.
    """
    image = np.asarray(render(by_name("tc01_old_tom_clean")).convert("RGB"))
    if brightness is None:
        return image
    scale = brightness / float(image.max())
    return (image.astype(np.float32) * scale).clip(0, 255).astype(np.uint8)


def _dim(image: np.ndarray, factor: float) -> np.ndarray:
    return degrade.dim(image, factor)


# --------------------------------------------------------------------------------------
# Defect one: exposure must never reward darkness
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("factor", [0.9, 0.75, 0.5, 0.3, 0.15])
def test_dimming_an_image_never_improves_its_exposure_score(factor: float) -> None:
    """The regression, stated as the direction the bug went.

    Under the old ceiling a bright label scored below a dimmer copy of itself, so the
    fix is not "the number is now higher" — it is that the function is monotonic in
    brightness. Any future ceiling breaks this at some factor.
    """
    bright = _label(250)
    dimmed = _dim(bright, factor)
    assert quality.exposure_score(dimmed) <= quality.exposure_score(bright)


def test_a_bright_label_scores_perfectly_rather_than_being_penalised() -> None:
    """Cream, white and foil labels are the normal case, not a defect.

    The rendered label's ground is cream at 250 — what a foil-stamped white label looks
    like to a camera. Scoring it as a problem sends an agent chasing a better
    photograph of a fine one.
    """
    assert quality.exposure_score(_label()) == 1.0
    assert quality.exposure_score(np.full((400, 400, 3), 255, dtype=np.uint8)) == 1.0


def test_exposure_is_monotonic_across_the_whole_brightness_range() -> None:
    """Brighter is never worse, at every step from black to white."""
    scores = [
        quality.exposure_score(np.full((400, 400, 3), level, dtype=np.uint8))
        for level in range(0, 256, 15)
    ]
    assert scores == sorted(scores)


def test_a_genuinely_dark_photograph_is_still_penalised() -> None:
    """Removing the ceiling must not remove the floor.

    An underexposed photograph is a real problem with a real retake reason. If this
    fails, the fix has gone too far and the tool has stopped noticing dark images.
    """
    assert quality.exposure_score(_dim(_label(), 0.06)) < thresholds.HOPELESS


def test_no_ceiling_can_exist_because_the_score_saturates_and_stays_there() -> None:
    """The constant that used to exist must stay gone — asserted as behaviour.

    The earlier version searched `dir(thresholds)` for a name containing EXPOSURE and
    CEIL/MAX/OVER, and its pass condition was an empty match set. A ceiling
    reintroduced as `BRIGHT_LIMIT`, `GLARE_MEAN_CAP`, or a literal in `exposure_score`
    would satisfy it — and a name grep whose empty result is the pass is exactly the
    shape of check this file exists to distrust.

    What actually rules a ceiling out is that the score reaches 1.0 at the floor and
    never comes back down, all the way to pure white. No ceiling of any name, in any
    module, can be in force while that holds.
    """
    at_floor = int(thresholds.EXPOSURE_FLOOR)
    scores = {
        level: quality.exposure_score(np.full((64, 64, 3), level, dtype=np.uint8))
        for level in range(at_floor, 256)
    }
    assert set(scores.values()) == {1.0}, {
        level: score for level, score in scores.items() if score != 1.0
    }


# --------------------------------------------------------------------------------------
# Defect two: blur must separate degrees of blur
# --------------------------------------------------------------------------------------


def _blurred(radius: float) -> np.ndarray:
    """Blurred through the same code path the TC-11..TC-14 fixtures use.

    `degrade.blur` is what produces `tc14_blur_hopeless` at radius 12. Reusing it means
    this test and the golden set are measuring the same degradation rather than two
    similar-looking ones.
    """
    if radius <= 0:
        return _label()
    return degrade.blur(_label(), radius)


def test_a_slightly_soft_image_scores_above_an_illegible_one() -> None:
    """The regression: a linear scale collapsed both to zero.

    "Slightly soft — parts may not be readable" and "too blurry to read, retake it" are
    different messages leading to different actions. On a linear scale the tool could
    not tell them apart, so every soft image got the harshest advice.
    """
    soft = quality.blur_score(_blurred(2))
    illegible = quality.blur_score(_blurred(12))
    assert soft > illegible
    assert soft - illegible > 0.2, (soft, illegible)


def test_blur_score_decreases_monotonically_with_radius() -> None:
    """More blur is never scored as sharper, at any radius."""
    scores = [quality.blur_score(_blurred(r)) for r in (0, 1, 2, 4, 8, 16)]
    assert scores == sorted(scores, reverse=True), scores


def test_a_sharp_label_scores_at_the_top_of_the_range() -> None:
    assert quality.blur_score(_label()) == 1.0


def test_a_hopelessly_blurred_label_falls_below_the_pre_gate() -> None:
    """LP-321: hopeless means zero model calls, so the score has to actually get there."""
    assert quality.blur_score(_blurred(16)) < thresholds.HOPELESS


def _directional_gradient_variance(image: np.ndarray) -> float:
    """The quantity the scorer scores, measured independently of how it scores it.

    This mirrors the *measurement* — greyscale, contrast stretch, σ=2 pre-smooth, then
    the smallest directional gradient variance over the eight sampled orientations — and
    deliberately not the *scale*, which is what the tests below are about. Without a
    variance to compare against there is no way to say anything about the shape of the
    curve, and asserting the shape is the whole point.

    It is written out here rather than imported from `quality` so that the two are
    independent. A test that called the scorer's own helper for both sides would compare
    the implementation with itself and hold for any scale whatsoever, which is the exact
    failure the parametrized test below was rewritten to escape.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY).astype(np.float32)
    low, high = float(gray.min()), float(gray.max())
    if high - low > 1.0:
        gray = np.clip((gray - low) * (255.0 / (high - low)), 0.0, 255.0)
    gray = np.asarray(cv2.GaussianBlur(gray, (0, 0), 2.0), dtype=np.float32)
    gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    return min(
        float((gx * np.cos(theta) + gy * np.sin(theta)).var())
        for theta in (np.pi * k / 8 for k in range(8))
    )


#: Radii whose variances land strictly inside the band, so no score is clipped at 0 or
#: 1 and every comparison is about the curve rather than about the clamp. Measured on the
#: current operator: 1129, 926, 709, 533, against bounds of 110 and 1200.
_UNCLIPPED_RADII = (1.0, 2.0, 3.0, 4.0)

#: The tolerance the parametrized assertion below allows. Named because the teeth test
#: measures against this number rather than against a second, looser one of its own —
#: "a linear scale would not satisfy that assertion" is a claim about *that* tolerance.
_SCALE_TOLERANCE = 0.01


def _linear_score(variance: float) -> float:
    """What a linear normalization would produce — the shipped defect, on today's measure."""
    return float(
        np.clip(variance / thresholds.SHARP_GRADIENT_VARIANCE, 0.0, 1.0)
    )


#: Ordered pairs, enumerated rather than filtered at run time. A `pytest.skip` for the
#: reversed and equal cases would report ten skips per run, and a suite that routinely
#: prints skips is one where a real skip goes unnoticed.
_UNCLIPPED_PAIRS = list(itertools.combinations(_UNCLIPPED_RADII, 2))


@pytest.mark.parametrize(("sharper", "blurrier"), _UNCLIPPED_PAIRS, ids=str)
def test_the_blur_score_moves_with_the_logarithm_of_the_variance(
    sharper: float, blurrier: float
) -> None:
    """The scale itself, asserted by calling the scorer on real images.

    An earlier version of this test asserted
    `log(sqrt(S*B)/B) / log(S/B) == 0.5`, which is a mathematical identity — true for
    every positive S != B, checked at (100,1), (7,3), (1e6,2) — and never called
    `blur_score` at all. Rewriting the scale as perfectly linear left it green. That is
    the `or True` genus, inside the file whose job is to pin a scale defect.

    What actually characterises a log scale is that a *ratio* of variances maps to a
    *difference* of scores, with the span as the constant. That holds for every pair
    below, and it is false for any linear normalization — which is asserted directly,
    over these same pairs, by the test underneath.

    Rewritten for the eight-orientation measure that replaced the Laplacian. The
    relation is a property of the *scale*, so it survived the operator change intact;
    only the quantity being fed into it and the two constants naming the bounds moved.
    """
    sharp_image, blurred_image = _blurred(sharper), _blurred(blurrier)
    sharp_variance = _directional_gradient_variance(sharp_image)
    blurred_variance = _directional_gradient_variance(blurred_image)

    span = np.log10(
        thresholds.SHARP_GRADIENT_VARIANCE / thresholds.BLUR_HOPELESS_VARIANCE
    )
    expected_gap = np.log10(sharp_variance / blurred_variance) / span
    actual_gap = quality.blur_score(sharp_image) - quality.blur_score(blurred_image)

    assert actual_gap == pytest.approx(expected_gap, abs=_SCALE_TOLERANCE), (
        f"variances {sharp_variance:.1f} -> {blurred_variance:.1f}"
    )


def test_a_linear_scale_would_fail_the_test_above() -> None:
    """The teeth. If a linear scorer satisfied that relation, it would prove nothing.

    Run the same comparison against a linear normalization, on *every* pair the test
    above uses, and show it misses the tolerance each time — so the assertion is
    discriminating between two scales rather than being satisfied by both.

    Over every pair, not one hand-picked pair, and that is load-bearing rather than
    thorough. On this measure a linear scale and a log scale happen to agree almost
    exactly at radius 1 against radius 8 — they cross there, missing by 0.002 — so a
    teeth test written against a single pair could sit on the crossing point and quietly
    certify that the two scales are indistinguishable. Whether any given pair lands near
    a crossing is an accident of the images; that every pair the assertion above is
    parametrized over stays clear of one is the thing worth pinning.
    """
    span = np.log10(
        thresholds.SHARP_GRADIENT_VARIANCE / thresholds.BLUR_HOPELESS_VARIANCE
    )

    misses = []
    for sharper, blurrier in _UNCLIPPED_PAIRS:
        sharp_variance = _directional_gradient_variance(_blurred(sharper))
        blurred_variance = _directional_gradient_variance(_blurred(blurrier))
        expected_gap = np.log10(sharp_variance / blurred_variance) / span
        linear_gap = _linear_score(sharp_variance) - _linear_score(blurred_variance)
        misses.append((sharper, blurrier, abs(linear_gap - expected_gap)))

    indistinguishable = [
        (a, b, miss) for a, b, miss in misses if miss <= _SCALE_TOLERANCE
    ]
    assert indistinguishable == [], (
        f"a linear scale satisfies the log relation at these pairs, so the assertion "
        f"above is not discriminating there: {indistinguishable}"
    )
    # And not merely outside the tolerance by a rounding error: the two scales are
    # visibly different answers over this range, not two spellings of one answer.
    assert max(miss for _, _, miss in misses) > 0.15, misses


def test_degrees_of_blur_do_not_collapse_into_one_answer() -> None:
    """The consequence the log scale exists to prevent, shown on the real images.

    "Slightly soft — parts may not be readable" and "too blurry to read, retake it" are
    different messages leading to different actions. The original defect made them the
    same message: on the raw Laplacian, which spans four decades, a linear normalization
    put radius 2 and radius 12 both at ~0.0 and every soft image got the harshest advice.

    **This no longer asserts that a linear scale would collapse them, because on today's
    measure it would not.** The σ=2 pre-smooth costs range at the sharp end — a clean
    label drops from 10825 to 1255 — so the measure now spans about one decade rather
    than four, and a linear normalization of it separates radius 6 from radius 12 too. It
    is still the wrong scale, and the test above is where that is now shown. Asserting a
    collapse that does not happen would be asserting something false about the code, so
    what is pinned here instead is the property the collapse was a violation *of*: a
    graded response, with the bands an agent actually reads coming out distinct.
    """
    scores = [quality.blur_score(_blurred(r)) for r in (0, 1, 2, 3, 4, 5, 6, 8)]

    # Never goes UP as the image gets softer. Unconditional — an inversion would be a
    # broken measure at any point on the curve.
    assert all(a >= b for a, b in itertools.pairwise(scores)), scores

    # No plateau BELOW the ceiling. The ceiling itself is allowed to hold more than one
    # radius, and on Linux it does: radius 0 and radius 1 both score exactly 1.0, where
    # macOS separates them. That is a difference between font rasterizers, not between
    # degrees of blur — both images are sharp, the score is clamped at 1.0, and demanding
    # a gap between "sharp" and "sharp" made CI red on every commit for eight commits
    # while the property under test was never in danger.
    #
    # What a collapse would actually look like is two DIFFERENT degrees of real blur
    # landing in one bucket, which is what this now pins.
    below_ceiling = [s for s in scores if s < 1.0]
    assert len(below_ceiling) >= 5, f"the measure saturates too far down the curve: {scores}"
    assert all(
        a - b > 0.01 for a, b in itertools.pairwise(below_ceiling)
    ), below_ceiling
    # …and the response uses the range rather than piling everything at one end.
    assert max(scores) - min(scores) > 0.75, scores

    # The bands the agent reads, which is where a collapse would actually be felt.
    soft, illegible = quality.assess(_blurred(6)), quality.assess(_blurred(12))
    assert (soft.verdict, illegible.verdict) == ("degraded", "hopeless")
    assert soft.reason != illegible.reason
    assert soft.reason is not None and "slightly soft" in soft.reason
    assert illegible.reason is not None and "too blurry" in illegible.reason


# --------------------------------------------------------------------------------------
# The two measurements stay independent
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-13")
def test_a_dark_image_is_reported_as_dark_rather_than_as_blurry() -> None:
    """Contrast is normalised away before the Laplacian, and that is load-bearing.

    Laplacian values scale linearly with contrast, so variance scales with its square —
    meaning a merely *dark* image measures as blurry. Without the stretch the retake
    reason would tell an agent to hold the camera steadier when the real problem is the
    light, and the retaken photograph would be just as unusable.
    """
    dark_but_sharp = _dim(_label(), 0.06)
    assert quality.blur_score(dark_but_sharp) > thresholds.DEGRADED
    assert quality.exposure_score(dark_but_sharp) < thresholds.HOPELESS
    reason = quality.assess(dark_but_sharp).reason
    assert reason is not None and "dark" in reason


def test_the_retake_reason_names_the_single_worst_problem() -> None:
    """An agent needs to know what to ask for, not a diagnostic report."""
    reason = quality.assess(_dim(_label(), 0.05)).reason
    assert reason is not None
    assert reason.count(".") <= 3


@pytest.mark.parametrize(
    ("image_factory", "expected_word"),
    [
        (lambda: _dim(_label(), 0.05), "dark"),
        (lambda: _blurred(12), "blurry"),
    ],
    ids=["underexposed", "out-of-focus"],
)
def test_each_kind_of_unusable_image_gets_its_own_advice(
    image_factory: object, expected_word: str
) -> None:
    """Naming the wrong problem sends the agent to fix the wrong thing.

    Radius 12 is the `tc14_blur_hopeless` preset — the calibration point the thresholds
    were set against.
    """
    report = quality.assess(image_factory())  # type: ignore[operator]
    assert report.verdict == "hopeless"
    assert report.reason is not None
    assert expected_word in report.reason


@pytest.mark.tc("TC-12")
def test_glare_is_counted_by_the_glare_score_and_not_by_exposure() -> None:
    """Blown-out highlights are real, and they belong to exactly one measurement.

    Counting them twice is what produced the exposure ceiling in the first place: a
    flashed label was penalised once for the glare and again for being bright.
    """
    flashed = degrade.glare(_label(), intensity=1.0, radius=0.4)
    assert quality.glare_score(flashed) < 1.0
    assert quality.exposure_score(flashed) == 1.0
