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

import numpy as np
import pytest

from api.pipeline import quality as Q
from api.rules import thresholds as T
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
    assert Q.exposure_score(dimmed) <= Q.exposure_score(bright)


def test_a_bright_label_scores_perfectly_rather_than_being_penalised() -> None:
    """Cream, white and foil labels are the normal case, not a defect.

    The rendered label's ground is cream at 250 — what a foil-stamped white label looks
    like to a camera. Scoring it as a problem sends an agent chasing a better
    photograph of a fine one.
    """
    assert Q.exposure_score(_label()) == 1.0
    assert Q.exposure_score(np.full((400, 400, 3), 255, dtype=np.uint8)) == 1.0


def test_exposure_is_monotonic_across_the_whole_brightness_range() -> None:
    """Brighter is never worse, at every step from black to white."""
    scores = [
        Q.exposure_score(np.full((400, 400, 3), level, dtype=np.uint8))
        for level in range(0, 256, 15)
    ]
    assert scores == sorted(scores)


def test_a_genuinely_dark_photograph_is_still_penalised() -> None:
    """Removing the ceiling must not remove the floor.

    An underexposed photograph is a real problem with a real retake reason. If this
    fails, the fix has gone too far and the tool has stopped noticing dark images.
    """
    assert Q.exposure_score(_dim(_label(), 0.06)) < T.HOPELESS


def test_the_thresholds_module_declares_no_exposure_ceiling() -> None:
    """The constant that used to exist must stay gone.

    Pinned on the module rather than on behaviour so that reintroducing the ceiling is
    caught the moment the constant reappears, before any scoring logic uses it.
    """
    ceiling_names = [
        name
        for name in dir(T)
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
    soft = Q.blur_score(_blurred(2))
    illegible = Q.blur_score(_blurred(12))
    assert soft > illegible
    assert soft - illegible > 0.2, (soft, illegible)


def test_blur_score_decreases_monotonically_with_radius() -> None:
    """More blur is never scored as sharper, at any radius."""
    scores = [Q.blur_score(_blurred(r)) for r in (0, 1, 2, 4, 8, 16)]
    assert scores == sorted(scores, reverse=True), scores


def test_a_sharp_label_scores_at_the_top_of_the_range() -> None:
    assert Q.blur_score(_label()) == 1.0


def test_a_hopelessly_blurred_label_falls_below_the_pre_gate() -> None:
    """LP-321: hopeless means zero model calls, so the score has to actually get there."""
    assert Q.blur_score(_blurred(16)) < T.HOPELESS


def test_the_blur_scale_is_logarithmic_rather_than_linear() -> None:
    """The scale itself, asserted rather than inferred.

    Halving the Laplacian variance moves a log score by a constant, not by a constant
    fraction of the sharp value. Testing the shape means a future rewrite that keeps
    the endpoints but straightens the curve is caught.
    """
    span = np.log10(T.SHARP_LAPLACIAN_VARIANCE / T.BLUR_HOPELESS_VARIANCE)
    midpoint_variance = np.sqrt(T.SHARP_LAPLACIAN_VARIANCE * T.BLUR_HOPELESS_VARIANCE)
    expected = np.log10(midpoint_variance / T.BLUR_HOPELESS_VARIANCE) / span
    assert expected == pytest.approx(0.5, abs=0.01)


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
    assert Q.blur_score(dark_but_sharp) > T.DEGRADED
    assert Q.exposure_score(dark_but_sharp) < T.HOPELESS
    reason = Q.assess(dark_but_sharp).reason
    assert reason is not None and "dark" in reason


def test_the_retake_reason_names_the_single_worst_problem() -> None:
    """An agent needs to know what to ask for, not a diagnostic report."""
    reason = Q.assess(_dim(_label(), 0.05)).reason
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
    report = Q.assess(image_factory())  # type: ignore[operator]
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
    assert Q.glare_score(flashed) < 1.0
    assert Q.exposure_score(flashed) == 1.0
