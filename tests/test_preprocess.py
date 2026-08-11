"""Photometric preprocessing (LP-190, LP-191, IMG-2, IMG-3).

The load-bearing tests here are the ones asserting what preprocessing does *not* do. A
pass that quietly improved every image would erase the evidence the warning-prominence
rules depend on, and it would do it invisibly.
"""

import numpy as np
import pytest

from api.pipeline import preprocess, quality
from api.rules import thresholds as T
from fixtures.generator import degrade
from fixtures.generator.catalog import by_name
from fixtures.generator.render import render


@pytest.fixture(scope="module")
def clean() -> np.ndarray:
    return np.array(render(by_name("tc01_old_tom_clean")))


def gray_of(image: np.ndarray) -> np.ndarray:
    import cv2

    return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)


# --- TC-13: dim but recoverable ----------------------------------------------------------

@pytest.mark.tc("TC-13")
def test_a_dim_photo_is_lifted(clean: np.ndarray) -> None:
    dim = degrade.dim(clean, 0.30)
    assert preprocess.preprocess(dim).exposure_normalized


@pytest.mark.tc("TC-13")
def test_lifting_actually_improves_the_exposure_score(clean: np.ndarray) -> None:
    dim = degrade.dim(clean, 0.30)
    result = preprocess.preprocess(dim)
    assert result.quality_after.exposure > result.quality_before.exposure


@pytest.mark.tc("TC-13")
@pytest.mark.parametrize("factor", [0.20, 0.30, 0.45])
def test_a_recoverable_photo_ends_up_readable(clean: np.ndarray, factor: float) -> None:
    result = preprocess.preprocess(degrade.dim(clean, factor))
    assert result.quality_after.verdict != "hopeless"


@pytest.mark.tc("TC-13")
def test_lifting_does_not_destroy_sharpness(clean: np.ndarray) -> None:
    """Aggressive local contrast turns sensor noise into speckle, which the blur measure
    then reads as detail — a score that goes up while legibility goes down."""
    dim = degrade.dim(clean, 0.30)
    result = preprocess.preprocess(dim)
    assert quality.blur_score(result.image) >= quality.blur_score(dim) - 0.1


# --- the rule that keeps a buried warning buried --------------------------------------------

@pytest.mark.tc("TC-06")
def test_a_well_exposed_photo_is_not_touched(clean: np.ndarray) -> None:
    """TC-06 is a warning printed pale on cream. If preprocessing lifted contrast on every
    image, that violation would arrive at the extractor looking perfectly legible and be
    reported as a pass. Enhancement is remedial or it is a false-pass path."""
    result = preprocess.preprocess(clean)
    assert not result.exposure_normalized
    assert np.array_equal(result.image, clean)


@pytest.mark.tc("TC-06")
def test_a_buried_warning_keeps_its_low_contrast(clean: np.ndarray) -> None:
    buried = np.array(render(by_name("tc06_buried_warning")))
    result = preprocess.preprocess(buried)

    def spread(image: np.ndarray) -> float:
        band = image[int(image.shape[0] * 0.62) :]
        return float(band.max()) - float(band.min())

    assert spread(result.image) == pytest.approx(spread(buried), abs=12)


def test_the_normalization_bar_is_the_exposure_floor(clean: np.ndarray) -> None:
    """One number, not two. `exposure_score` is mean luminance over EXPOSURE_FLOOR, so a
    score below 1.0 is exactly 'dimmer than a well-lit label' and needs no new knob."""
    for factor in (1.0, 0.6, 0.3, 0.1):
        image = clean if factor == 1.0 else degrade.dim(clean, factor)
        assessment = quality.assess(image)
        expected = float(image.mean()) < T.EXPOSURE_FLOOR
        assert preprocess.needs_exposure_normalization(assessment) is expected


# --- TC-12: glare, enhanced but never invented --------------------------------------------

@pytest.mark.tc("TC-12")
def test_a_blown_pixel_is_never_filled_in(clean: np.ndarray) -> None:
    """The load-bearing test of LP-191. A pixel at 255 carries no information about what
    was under it, so anything written there is fabrication — and *confident* fabrication,
    which the extractor would read as clean text and the verdict would carry as a pass."""
    glared = degrade.glare(clean)
    blown_before = gray_of(glared) >= preprocess.BLOWN_LEVEL

    out = preprocess.enhance_glare(glared)

    assert np.array_equal(out[blown_before], glared[blown_before])


@pytest.mark.tc("TC-12")
def test_enhancement_never_reduces_the_blown_area(clean: np.ndarray) -> None:
    """Recovering blown pixels is the signature of inpainting. If this count ever drops,
    something started painting label content into a region nobody photographed."""
    glared = degrade.glare(clean)
    out = preprocess.enhance_glare(glared)
    assert preprocess.blown_fraction(out) >= preprocess.blown_fraction(glared)


@pytest.mark.tc("TC-12")
def test_the_glare_mask_covers_the_highlight(clean: np.ndarray) -> None:
    """Defaults put the patch over the lower portion, where the warning sits."""
    glared = degrade.glare(clean)
    mask = preprocess.glare_mask(glared)
    bottom_half = mask[mask.shape[0] // 2 :]
    assert bottom_half.mean() > mask[: mask.shape[0] // 2].mean()


@pytest.mark.tc("TC-12")
def test_a_clean_label_has_no_glare_mask(clean: np.ndarray) -> None:
    assert preprocess.glare_mask(clean).sum() == 0


@pytest.mark.tc("TC-12")
def test_glare_recovery_runs_on_a_real_highlight(clean: np.ndarray) -> None:
    result = preprocess.preprocess(degrade.glare(clean))
    assert result.glare_enhanced
    assert result.glare_fraction > 0.0


def test_a_stray_specular_pixel_is_not_chased(clean: np.ndarray) -> None:
    """A speck on a foil capsule is not glare, and running a local operator over the whole
    image to chase it changes every pixel for no gain."""
    speck = clean.copy()
    speck[10:14, 10:14] = 255
    assert not preprocess.preprocess(speck).glare_enhanced


@pytest.mark.tc("TC-12")
def test_the_note_says_nothing_was_painted_in(clean: np.ndarray) -> None:
    """An agent reading the log has to be able to tell recovery from invention."""
    notes = " ".join(preprocess.preprocess(degrade.glare(clean)).notes)
    assert "nothing was painted into it" in notes


@pytest.mark.tc("TC-12")
def test_glare_recovery_leaves_the_dry_half_of_the_label_alone(clean: np.ndarray) -> None:
    """TC-12's whole point: the warning under the flash is lost, the brand above it is
    not. If recovery smeared the top of the label the brand would be collateral."""
    glared = degrade.glare(clean)
    out = preprocess.enhance_glare(glared)
    top = slice(0, int(clean.shape[0] * 0.25))
    assert quality.blur_score(out[top]) >= quality.blur_score(glared[top]) - 0.05


# --- honesty of the report --------------------------------------------------------------------

def test_the_report_keeps_the_scores_the_photo_arrived_with(clean: np.ndarray) -> None:
    """After lifting, the scores necessarily look better. Reporting those to an agent
    would tell them the photograph was fine when it was not."""
    result = preprocess.preprocess(degrade.dim(clean, 0.15))
    assert result.quality_before.exposure < result.quality_after.exposure
    assert result.quality_before.verdict == "degraded"
    assert result.quality_after.verdict == "ok"


def test_every_pass_explains_itself(clean: np.ndarray) -> None:
    for image in (clean, degrade.dim(clean, 0.3), degrade.on_surface(clean, degrees=25.0)):
        assert preprocess.preprocess(image).notes


def test_an_untouched_image_reports_no_change(clean: np.ndarray) -> None:
    assert not preprocess.preprocess(clean).changed


def test_a_corrected_image_reports_the_change(clean: np.ndarray) -> None:
    assert preprocess.preprocess(degrade.rotate(clean, 9.0)).changed


# --- colour is evidence ---------------------------------------------------------------------------

def test_normalization_leaves_hue_alone(clean: np.ndarray) -> None:
    """Equalising RGB channels independently shifts colour. A warning printed pale grey
    and one printed black are different compliance outcomes."""
    tinted = np.clip(clean.astype(np.int16) * np.array([1.0, 0.85, 0.7]), 0, 255).astype(
        np.uint8
    )
    dim = degrade.dim(tinted, 0.3)
    out = preprocess.normalize_exposure(dim)
    assert out[..., 0].mean() > out[..., 1].mean() > out[..., 2].mean()


# --- ordering and determinism -----------------------------------------------------------

def test_geometry_runs_before_light(clean: np.ndarray) -> None:
    """A CLAHE tile straddling the label edge and the desk it is lying on is measuring two
    scenes. Rectifying first means the tiles see label."""
    photo = degrade.dim(degrade.on_surface(clean, degrees=30.0), 0.35)
    result = preprocess.preprocess(photo)
    assert result.perspective_applied
    assert result.image.shape != photo.shape


def test_the_pass_is_deterministic(clean: np.ndarray) -> None:
    dim = degrade.dim(clean, 0.3)
    assert np.array_equal(preprocess.preprocess(dim).image, preprocess.preprocess(dim).image)


def test_grayscale_input_is_handled(clean: np.ndarray) -> None:
    """Ingest emits mode 'L' for a greyscale source; preprocessing must not assume RGB."""
    import cv2

    gray = cv2.cvtColor(degrade.dim(clean, 0.3), cv2.COLOR_RGB2GRAY)
    assert preprocess.normalize_exposure(gray).shape == gray.shape
