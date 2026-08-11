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
        degrade.blur(clean, 20.0), REGIONS[FieldName.GOVERNMENT_WARNING]
    )
    assert assessment.has_content
    assert assessment.verdict == "hopeless"


@pytest.mark.tc("TC-14")
def test_small_print_goes_illegible_before_the_headline_does(clean: np.ndarray) -> None:
    """Which is the whole argument for scoring regions. The warning statement is the
    smallest type on the label and the first thing a soft photo loses, and it is also the
    one field where being wrong is disqualifying — so a global score that averages it
    against a 72-point brand name is measuring the wrong thing."""
    soft = degrade.blur(clean, 20.0)
    warning = quality.assess_region(soft, REGIONS[FieldName.GOVERNMENT_WARNING])
    brand = quality.assess_region(soft, REGIONS[FieldName.BRAND_NAME])
    assert warning.blur < brand.blur
    assert not warning.legible and brand.legible


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


@pytest.mark.parametrize(
    ("name", "box"),
    [
        ("zero-area, mid-frame", BoundingBox(x0=0.5, y0=0.5, x1=0.5, y1=0.5)),
        ("flush with the bottom", BoundingBox(x0=0.0, y0=1.0, x1=1.0, y1=1.0)),
        ("flush with the right", BoundingBox(x0=1.0, y0=0.0, x1=1.0, y1=1.0)),
        ("the far corner", BoundingBox(x0=1.0, y0=1.0, x1=1.0, y1=1.0)),
        ("inverted", BoundingBox(x0=0.0, y0=0.8, x1=1.0, y1=0.2)),
    ],
)
def test_a_degenerate_box_never_produces_an_empty_crop(
    clean: np.ndarray, name: str, box: BoundingBox
) -> None:
    """Boxes come from the extractor and `BoundingBox` permits 1.0 on either edge. An
    empty slice reaches OpenCV as an assertion failure, which leaves here as a 500 rather
    than as anything in the error taxonomy."""
    assert quality.crop(clean, box).size > 0, name


@pytest.mark.parametrize(
    ("name", "box"),
    [
        ("zero-area, mid-frame", BoundingBox(x0=0.5, y0=0.5, x1=0.5, y1=0.5)),
        ("flush with the bottom", BoundingBox(x0=0.0, y0=1.0, x1=1.0, y1=1.0)),
        ("a two-pixel sliver", BoundingBox(x0=0.5, y0=0.5, x1=0.502, y1=0.502)),
    ],
)
def test_an_unmeasurable_region_fails_closed(
    clean: np.ndarray, name: str, box: BoundingBox
) -> None:
    """A box too small to measure is a broken box, not a reading.

    This set is what forces a field to Unreadable, so the default has to be "we could not
    check this" rather than "it looked fine" — the second is a false pass wearing the
    clothes of a successful check.
    """
    assessment = quality.assess_region(clean, box)
    assert assessment.verdict == "hopeless", name
    assert not assessment.legible
    assert assessment.reason and "could not be measured" in assessment.reason


def test_a_broken_box_puts_its_field_in_the_illegible_set(clean: np.ndarray) -> None:
    broken = {FieldName.GOVERNMENT_WARNING: BoundingBox(x0=0.0, y0=1.0, x1=1.0, y1=1.0)}
    assert quality.illegible_regions(clean, broken) == {FieldName.GOVERNMENT_WARNING}


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
    """Shape is not asserted to be unchanged: a label composited onto the desk it was
    photographed on is a bigger frame than the label, which is the point of that fixture."""
    for preset in degrade.PRESETS:
        out = degrade.apply_preset(clean, preset)
        assert out.ndim == 3 and out.dtype == np.uint8 and out.size > 0


def test_unknown_preset_raises() -> None:
    with pytest.raises(KeyError):
        degrade.apply_preset(np.zeros((10, 10, 3), np.uint8), "nope")


# --- blank must be earned, not assumed --------------------------------------------------

@pytest.mark.tc("TC-06")
@pytest.mark.parametrize("contrast", [0.12, 0.10, 0.07])
def test_a_faint_warning_is_never_called_blank(contrast: float) -> None:
    """The band where WARN-5 violations live.

    A warning printed this faintly measures 0.09–0.11 relative contrast; blank stock under
    heavy sensor noise measures 0.106. They overlap, so nothing can separate them — and
    calling one of them blank would report a buried warning as Missing *and* mark the
    field legible on the way past.
    """
    spec = by_name("tc01_old_tom_clean").with_(warning_contrast=contrast)
    image = np.array(render(spec))
    assessment = quality.assess_region(image, REGIONS[FieldName.GOVERNMENT_WARNING])

    assert assessment.verdict != "blank"
    assert not assessment.legible


@pytest.mark.tc("TC-06")
@pytest.mark.parametrize("contrast", [1.0, 0.5, 0.35, 0.2])
def test_low_contrast_alone_never_reads_as_blur(contrast: float) -> None:
    """Contrast is normalized away before the blur measure, and this is what proves it.

    A warning printed at a fifth of full contrast still scores a perfect 1.000 for
    sharpness, so "faint" and "soft" stay the separate problems they are and each retake
    reason names the one the agent can actually fix.
    """
    spec = by_name("tc01_old_tom_clean").with_(warning_contrast=contrast)
    image = np.array(render(spec))
    assert quality.assess_region(image, REGIONS[FieldName.GOVERNMENT_WARNING]).blur == 1.0


@pytest.mark.tc("TC-06")
def test_tc06s_shrunken_warning_is_unreadable_at_this_resolution(clean: np.ndarray) -> None:
    """TC-06 shrinks the warning to 45% as well as burying it, and at that size on a
    1400px render the strokes are ~10px — genuinely past reading.

    So the region comes back Unreadable rather than legible. That is a blunter answer than
    the prominence violation LP-211 wants to report, but it is the honest one and it still
    blocks approval. What matters here is which way it fails: not `blank`, which would
    read as Missing *and* mark the field legible.
    """
    buried = np.array(render(by_name("tc06_buried_warning")))
    assessment = quality.assess_region(buried, REGIONS[FieldName.GOVERNMENT_WARNING])

    assert assessment.has_content
    assert assessment.verdict == "hopeless"
    assert not assessment.legible


@pytest.mark.tc("TC-06")
def test_the_size_is_what_makes_it_unreadable_not_the_faintness(clean: np.ndarray) -> None:
    """Separating the two factors, because conflating them would hide a real defect in the
    measure. At full size and 35% contrast the region reads perfectly; at 45% size and full
    contrast it does not."""
    faint = np.array(render(by_name("tc01_old_tom_clean").with_(warning_contrast=0.35)))
    small = np.array(render(by_name("tc01_old_tom_clean").with_(warning_scale=0.45)))
    band = REGIONS[FieldName.GOVERNMENT_WARNING]

    assert quality.assess_region(faint, band).legible
    assert not quality.assess_region(small, band).legible


def test_genuinely_blank_stock_is_still_blank(clean: np.ndarray) -> None:
    """The other side. If everything faint became illegible, every field whose box
    included a margin would flag."""
    assert quality.assess_region(clean, BLANK).verdict == "blank"


# --- the region gate is never more optimistic than the pre-gate ---------------------------

@pytest.mark.tc("TC-14")
def test_no_region_of_a_rejected_image_reads_as_legible(clean: np.ndarray) -> None:
    """Contrast is stretched per region and variance is diluted by blank area, so a dense
    band of text scores better on its own than the picture containing it. Measured before
    this guard: whole image 0.000 and hopeless, warning region of that same image 0.307
    and legible. The global gate said nobody could read it and the region gate — the one
    that decides Unreadable — disagreed, in the unsafe direction."""
    wrecked = degrade.blur(clean, 16.0)
    assert quality.assess(wrecked).verdict == "hopeless"
    assert quality.illegible_regions(wrecked, REGIONS) == set(REGIONS)


def test_a_readable_image_still_gets_per_region_answers(clean: np.ndarray) -> None:
    """The clamp must not swallow the TC-12 case, where the picture is fine and one
    region is not."""
    glared = degrade.glare_over_warning(clean)
    assert quality.assess(glared).verdict != "hopeless"
    assert quality.illegible_regions(glared, REGIONS) == {FieldName.GOVERNMENT_WARNING}


def test_the_clamp_is_optional_so_a_region_can_be_scored_alone(clean: np.ndarray) -> None:
    wrecked = degrade.blur(clean, 16.0)
    alone = quality.assess_region(wrecked, REGIONS[FieldName.BRAND_NAME])
    clamped = quality.assess_region(
        wrecked, REGIONS[FieldName.BRAND_NAME], quality.assess(wrecked)
    )
    assert alone.blur == clamped.blur
    assert clamped.verdict == "hopeless"


# --- a hazard this wave cannot fix, pinned so it cannot be forgotten -----------------------

def test_the_fake_providers_evidence_bands_do_not_match_the_renderer() -> None:
    """A live trap for whoever wires region readability into the route.

    `api/provider/fake.py` places the government warning at y 0.66–0.88. The renderer puts
    it at 0.450–0.540, and everything below 0.62 is bare stock — so feeding the fake's box
    to `assess_region` lands on blank label, scores `blank`, and reads as legible. That is
    a false pass waiting for the wiring.

    This wave does not own `api/provider/**`, so this asserts the mismatch rather than
    fixing it. When the bands are corrected this test goes red, which is the point: it is
    a note that cannot be lost, and deleting it is a deliberate act.
    """
    from api.provider.fake import _APPROX_REGIONS

    fake = _APPROX_REGIONS[FieldName.GOVERNMENT_WARNING]
    real = REGIONS[FieldName.GOVERNMENT_WARNING]
    assert fake.y0 > real.y1, (
        "api/provider/fake.py now agrees with the renderer — delete this test and the "
        "warning beside it in api/pipeline/limitations.py::WIRING"
    )
