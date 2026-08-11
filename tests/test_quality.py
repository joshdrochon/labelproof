"""Image quality scoring and the pre-gate.

Every test runs against a generated label with a known degradation applied, so the
expectation is grounded in what was actually done to the pixels.
"""

import numpy as np
import pytest

from api.models import BoundingBox, FieldName
from api.pipeline import quality
from api.rules import thresholds as T
from fixtures.generator import degrade
from fixtures.generator.catalog import by_name
from fixtures.generator.layout import BLANK_BAND as BLANK
from fixtures.generator.layout import FIELD_BANDS as REGIONS
from fixtures.generator.render import render


@pytest.fixture(scope="module")
def clean() -> np.ndarray:
    return np.array(render(by_name("tc01_old_tom_clean")))


# --- the clean baseline -------------------------------------------------------------

def test_a_clean_render_scores_ok(clean: np.ndarray) -> None:
    assessment = quality.assess(clean)
    assert assessment.verdict == "ok"
    assert assessment.reason is None


def test_a_clean_render_is_not_skipped(clean: np.ndarray) -> None:
    assert not quality.should_skip_extraction(quality.assess(clean))


def test_scores_are_all_normalized(clean: np.ndarray) -> None:
    a = quality.assess(clean)
    for score in (a.blur, a.exposure, a.glare):
        assert 0.0 <= score <= 1.0


# --- TC-14: hopeless blur -------------------------------------------------------------

@pytest.mark.tc("TC-14")
def test_hopeless_blur_is_detected(clean: np.ndarray) -> None:
    assessment = quality.assess(degrade.blur(clean, 12.0))
    assert assessment.blur < T.HOPELESS
    assert assessment.verdict == "hopeless"


@pytest.mark.tc("TC-14")
def test_hopeless_blur_skips_the_model_entirely(clean: np.ndarray) -> None:
    """LP-321 — the pre-gate spends nothing and cannot produce a false pass."""
    assert quality.should_skip_extraction(quality.assess(degrade.blur(clean, 12.0)))


@pytest.mark.tc("TC-14")
def test_hopeless_blur_says_what_to_do(clean: np.ndarray) -> None:
    reason = quality.assess(degrade.blur(clean, 12.0)).reason
    assert reason and "blurry" in reason.lower()
    assert "retake" in reason.lower()


def test_mild_blur_is_still_worth_reading(clean: np.ndarray) -> None:
    """Degraded is not hopeless — a soft photo still gets extracted."""
    assert not quality.should_skip_extraction(quality.assess(degrade.blur(clean, 2.0)))


def test_blur_score_decreases_monotonically_with_radius(clean: np.ndarray) -> None:
    scores = [quality.blur_score(degrade.blur(clean, r)) for r in (1.0, 4.0, 8.0, 14.0)]
    assert scores == sorted(scores, reverse=True)


# --- TC-13: dim -----------------------------------------------------------------------

@pytest.mark.tc("TC-13")
def test_dim_lighting_lowers_the_exposure_score(clean: np.ndarray) -> None:
    assert quality.exposure_score(degrade.dim(clean, 0.25)) < quality.exposure_score(clean)


@pytest.mark.tc("TC-13")
def test_recoverable_dimness_is_not_hopeless(clean: np.ndarray) -> None:
    """Underexposed but recoverable — extract it, do not reject it."""
    assert not quality.should_skip_extraction(quality.assess(degrade.dim(clean, 0.5)))


@pytest.mark.tc("TC-13")
def test_near_black_is_hopeless_and_says_so(clean: np.ndarray) -> None:
    assessment = quality.assess(degrade.dim(clean, 0.04))
    assert assessment.verdict == "hopeless"
    assert assessment.reason and "dark" in assessment.reason.lower()


# --- TC-12: glare ----------------------------------------------------------------------

@pytest.mark.tc("TC-12")
def test_glare_lowers_the_glare_score(clean: np.ndarray) -> None:
    assert quality.glare_score(degrade.glare(clean)) < quality.glare_score(clean)


@pytest.mark.tc("TC-12")
def test_heavy_glare_reason_mentions_flash(clean: np.ndarray) -> None:
    """The agents' own workflow verb — retake without flash."""
    heavy = degrade.glare(clean, radius=0.9, intensity=1.0)
    assessment = quality.assess(heavy)
    if assessment.verdict == "hopeless":
        assert "flash" in (assessment.reason or "").lower()


def test_a_small_highlight_does_not_condemn_the_image(clean: np.ndarray) -> None:
    small = degrade.glare(clean, radius=0.05, intensity=0.8)
    assert not quality.should_skip_extraction(quality.assess(small))


# --- TC-11: angle -----------------------------------------------------------------------

@pytest.mark.tc("TC-11")
def test_rotation_is_measured(clean: np.ndarray) -> None:
    assert abs(quality.skew_degrees(degrade.rotate(clean, 8.0))) > 2.0


@pytest.mark.tc("TC-11")
def test_a_square_image_reports_near_zero_skew(clean: np.ndarray) -> None:
    assert abs(quality.skew_degrees(clean)) < 2.0


@pytest.mark.tc("TC-11")
@pytest.mark.parametrize("degrees", [15.0, 30.0, 45.0])
def test_perspective_does_not_make_a_label_hopeless(clean: np.ndarray, degrees: float) -> None:
    """An angled but sharp photo is readable — correct it, do not reject it."""
    assert not quality.should_skip_extraction(quality.assess(degrade.perspective(clean, degrees)))


def test_cylinder_warp_keeps_the_label_readable(clean: np.ndarray) -> None:
    assert not quality.should_skip_extraction(quality.assess(degrade.cylinder(clean)))


# --- resolution -------------------------------------------------------------------------

def test_low_resolution_is_flagged_but_not_rejected(clean: np.ndarray) -> None:
    """A finding, never a route to a cheaper model — see JUDGMENT-LOG."""
    import cv2
    small = cv2.resize(clean, (400, 560))
    assessment = quality.assess(small)
    assert not assessment.resolution_ok
    assert assessment.verdict != "hopeless"
    assert assessment.reason and "resolution" in assessment.reason.lower()


# --- the pre-gate's honest limit ----------------------------------------------------------

@pytest.mark.tc("TC-15")
def test_a_sharp_non_label_image_passes_the_pre_gate(clean: np.ndarray) -> None:
    """The gate catches illegible, not wrong-subject. A cat photo scores fine."""
    rng = np.random.default_rng(0)
    photo = rng.integers(60, 200, size=(1400, 1000, 3), dtype=np.uint8)
    assert not quality.should_skip_extraction(quality.assess(photo))


# --- LP-192: quality judged per region, not per image ------------------------------------

def test_the_region_boxes_actually_land_on_their_text(clean: np.ndarray) -> None:
    """Guards every other region assertion in this file. If the renderer moves a block,
    these boxes stop containing text and the tests below would pass for the wrong
    reason — a green suite proving nothing."""
    for field, box in REGIONS.items():
        assert quality.assess_region(clean, box).has_content, field


def test_a_blank_area_is_blank_not_blurry(clean: np.ndarray) -> None:
    """Laplacian variance over bare label stock is legitimately near zero. Reporting that
    as 'too blurry to read' would flag every field whose box includes a margin."""
    assessment = quality.assess_region(clean, BLANK)
    assert assessment.verdict == "blank"
    assert assessment.legible


def test_every_region_of_a_clean_label_reads(clean: np.ndarray) -> None:
    assert quality.illegible_regions(clean, REGIONS) == set()


@pytest.mark.tc("TC-12")
def test_glare_over_the_warning_actually_covers_it(clean: np.ndarray) -> None:
    """The fixture guard for TC-12. A glare patch that missed the warning would leave the
    case testing nothing while looking like it passed."""
    glared = degrade.glare_over_warning(clean)
    warning = quality.assess_region(glared, REGIONS[FieldName.GOVERNMENT_WARNING])
    assert warning.glare < quality.assess_region(clean, REGIONS[FieldName.GOVERNMENT_WARNING]).glare


@pytest.mark.tc("TC-12")
def test_glare_on_the_warning_leaves_the_rest_of_the_label_readable(clean: np.ndarray) -> None:
    """The whole point of per-region scoring. One global number would call this image
    fine, because it *is* fine everywhere except the one place that matters most."""
    glared = degrade.glare_over_warning(clean)
    assert quality.illegible_regions(glared, REGIONS) == {FieldName.GOVERNMENT_WARNING}


@pytest.mark.tc("TC-12")
def test_the_whole_image_still_scores_ok_under_that_glare(clean: np.ndarray) -> None:
    """The image-level gate must NOT reject this. Rejecting it would throw away the five
    fields that are perfectly readable to protect the one that is not."""
    assert quality.assess(degrade.glare_over_warning(clean)).verdict != "hopeless"


@pytest.mark.tc("TC-12")
def test_the_region_reason_names_glare(clean: np.ndarray) -> None:
    glared = degrade.glare_over_warning(clean)
    reason = quality.assess_region(glared, REGIONS[FieldName.GOVERNMENT_WARNING]).reason
    assert reason and "glare" in reason.lower()


@pytest.mark.tc("TC-14")
def test_blurred_text_is_illegible_not_blank(clean: np.ndarray) -> None:
    """Edge density collapses to zero on text blurred past legibility. Calling that region
    blank would report a genuine TC-14 failure as an empty part of the label."""
    assessment = quality.assess_region(
        degrade.blur(clean, 12.0), REGIONS[FieldName.BRAND_NAME]
    )
    assert assessment.has_content
    assert assessment.verdict == "hopeless"


@pytest.mark.tc("TC-13")
def test_a_dim_region_is_reported_as_dark(clean: np.ndarray) -> None:
    assessment = quality.assess_region(
        degrade.dim(clean, 0.05), REGIONS[FieldName.GOVERNMENT_WARNING]
    )
    assert assessment.reason and "dark" in assessment.reason.lower()


def test_region_content_detection_survives_dim_light(clean: np.ndarray) -> None:
    """Relative contrast, not an absolute range: a dimly lit line of text is still text."""
    assert quality.assess_region(
        degrade.dim(clean, 0.1), REGIONS[FieldName.BRAND_NAME]
    ).has_content


def test_a_degenerate_box_does_not_crash(clean: np.ndarray) -> None:
    zero = BoundingBox(x0=0.5, y0=0.5, x1=0.5, y1=0.5)
    assert quality.crop(clean, zero).size > 0


def test_region_assessment_is_deterministic(clean: np.ndarray) -> None:
    box = REGIONS[FieldName.GOVERNMENT_WARNING]
    assert quality.assess_region(clean, box) == quality.assess_region(clean, box)


# --- determinism ---------------------------------------------------------------------------

def test_assessment_is_deterministic(clean: np.ndarray) -> None:
    assert quality.assess(clean) == quality.assess(clean)


def test_degradations_are_reproducible(clean: np.ndarray) -> None:
    """LP-123 — seeded, so a fixture regenerates byte for byte."""
    assert np.array_equal(degrade.glare(clean, seed=7), degrade.glare(clean, seed=7))


def test_every_preset_applies(clean: np.ndarray) -> None:
    for preset in degrade.PRESETS:
        assert degrade.apply_preset(clean, preset).shape == clean.shape


def test_unknown_preset_raises() -> None:
    with pytest.raises(KeyError):
        degrade.apply_preset(np.zeros((10, 10, 3), np.uint8), "nope")
