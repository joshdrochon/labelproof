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


def test_the_thresholds_module_declares_no_exposure_ceiling() -> None:
    """The constant that used to exist must stay gone.

    Pinned on the module rather than on behaviour so that reintroducing the ceiling is
    caught the moment the constant reappears, before any scoring logic uses it.
    """
    ceiling_names = [
        name
        for name in dir(thresholds)
        if "EXPOSURE" in name and ("CEIL" in name or "MAX" in name or "OVER" in name)
    ]
    assert ceiling_names == []


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


def _laplacian_variance(image: np.ndarray) -> float:
    """The quantity the scorer scores, measured independently of how it scores it.

    This mirrors the *measurement* — greyscale, contrast stretch, Laplacian variance —
    and deliberately not the *scale*, which is what the tests below are about. Without
    a variance to compare against there is no way to say anything about the shape of
    the curve, and asserting the shape is the whole point.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY).astype(np.float32)
    low, high = float(gray.min()), float(gray.max())
    if high - low > 1.0:
        gray = (gray - low) * (255.0 / (high - low))
    return float(cv2.Laplacian(gray.astype(np.uint8), cv2.CV_64F).var())


#: Radii whose variances land strictly inside the band, so no score is clipped at 0 or
#: 1 and every comparison is about the curve rather than about the clamp.
_UNCLIPPED_RADII = (1.0, 2.0, 3.0, 4.0)


def _linear_score(variance: float) -> float:
    """What a linear normalization would have produced — the shipped defect."""
    return float(
        np.clip(variance / thresholds.SHARP_LAPLACIAN_VARIANCE, 0.0, 1.0)
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
    below, and it is false for any linear normalization.
    """
    sharp_image, blurred_image = _blurred(sharper), _blurred(blurrier)
    sharp_variance = _laplacian_variance(sharp_image)
    blurred_variance = _laplacian_variance(blurred_image)

    span = np.log10(
        thresholds.SHARP_LAPLACIAN_VARIANCE / thresholds.BLUR_HOPELESS_VARIANCE
    )
    expected_gap = np.log10(sharp_variance / blurred_variance) / span
    actual_gap = quality.blur_score(sharp_image) - quality.blur_score(blurred_image)

    assert actual_gap == pytest.approx(expected_gap, abs=0.01), (
        f"variances {sharp_variance:.1f} -> {blurred_variance:.1f}"
    )


def test_a_linear_scale_would_fail_the_test_above() -> None:
    """The teeth. If a linear scorer satisfied that relation, it would prove nothing.

    Run the same comparison against a linear normalization and show it is wrong by a
    wide margin — so the assertion is discriminating between two scales rather than
    being satisfied by both.
    """
    span = np.log10(
        thresholds.SHARP_LAPLACIAN_VARIANCE / thresholds.BLUR_HOPELESS_VARIANCE
    )
    sharp_image, blurred_image = _blurred(1.0), _blurred(4.0)
    sharp_variance = _laplacian_variance(sharp_image)
    blurred_variance = _laplacian_variance(blurred_image)

    expected_gap = np.log10(sharp_variance / blurred_variance) / span
    linear_gap = _linear_score(sharp_variance) - _linear_score(blurred_variance)

    assert abs(linear_gap - expected_gap) > 0.2, (
        f"a linear scale is indistinguishable here: {linear_gap:.3f} vs {expected_gap:.3f}"
    )


def test_a_linear_scale_would_collapse_soft_and_illegible_together() -> None:
    """The consequence the log scale exists to prevent, shown on the real images.

    Under a linear normalization both radius 2 and radius 12 score near zero, so
    "slightly soft — parts may not be readable" and "too blurry to read, retake it"
    become the same answer and every soft image gets the harshest advice. The real
    scorer separates them; the linear one does not.
    """
    soft_variance = _laplacian_variance(_blurred(2.0))
    illegible_variance = _laplacian_variance(_blurred(12.0))

    assert _linear_score(soft_variance) - _linear_score(illegible_variance) < 0.1
    assert quality.blur_score(_blurred(2.0)) - quality.blur_score(_blurred(12.0)) > 0.4


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
