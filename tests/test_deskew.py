"""Geometric correction (LP-189, LP-201, IMG-1).

Every case runs against a generated label with a known transform applied, so the
expectation is grounded in what was actually done to the pixels rather than in a number
that happened to come out of the estimator.
"""

import cv2
import numpy as np
import pytest

from api.pipeline import deskew, quality
from api.rules import thresholds as T
from fixtures.generator import degrade
from fixtures.generator.catalog import by_name
from fixtures.generator.render import render


@pytest.fixture(scope="module")
def clean() -> np.ndarray:
    return np.array(render(by_name("tc01_old_tom_clean")))


def _ink(image: np.ndarray) -> int:
    """Dark pixels — a proxy for how much of the label's text survived a transform."""
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    return int((gray < 100).sum())


# --- skew estimation ------------------------------------------------------------------

@pytest.mark.tc("TC-11")
@pytest.mark.parametrize("degrees", [3.0, 8.0, -8.0, 15.0])
def test_rotation_is_measured_with_the_right_sign(clean: np.ndarray, degrees: float) -> None:
    """The estimate must be the inverse of the applied rotation, or correcting it moves
    the image the wrong way — a failure that looks like the estimator working."""
    assert deskew.estimate_skew(degrade.rotate(clean, degrees)) == pytest.approx(
        -degrees, abs=1.5
    )


def test_a_square_label_reports_no_skew(clean: np.ndarray) -> None:
    assert abs(deskew.estimate_skew(clean)) < 1.0


def test_featureless_image_reports_zero_rather_than_guessing() -> None:
    """0.0 here means 'no dominant orientation', not 'this image is square'."""
    assert deskew.estimate_skew(np.full((800, 600, 3), 200, np.uint8)) == 0.0


def test_the_estimate_does_not_depend_on_resolution(clean: np.ndarray) -> None:
    """A fixed Hough vote count silently stops finding lines as images shrink, so the
    same photo at two sizes would get two different answers."""
    tilted = degrade.rotate(clean, 6.0)
    half = cv2.resize(tilted, (tilted.shape[1] // 2, tilted.shape[0] // 2))
    assert deskew.estimate_skew(half) == pytest.approx(deskew.estimate_skew(tilted), abs=1.5)


def test_quality_and_deskew_report_the_same_angle(clean: np.ndarray) -> None:
    """One estimator. A quality report that disagreed with what preprocessing did would
    be worse than no report."""
    tilted = degrade.rotate(clean, 7.0)
    assert quality.skew_degrees(tilted) == deskew.estimate_skew(tilted)


# --- rotation correction ---------------------------------------------------------------

@pytest.mark.tc("TC-11")
@pytest.mark.parametrize("degrees", [4.0, 8.0, -12.0])
def test_a_tilted_label_is_levelled(clean: np.ndarray, degrees: float) -> None:
    result = deskew.correct(degrade.rotate(clean, degrees))
    assert result.rotation_deg != 0.0
    assert abs(deskew.estimate_skew(result.image)) < T.SKEW_CORRECTION_DEG


def test_a_square_label_is_left_completely_alone(clean: np.ndarray) -> None:
    """Resampling costs sharpness. Correcting a 0.2° tilt buys nothing and pays for it."""
    result = deskew.correct(clean)
    assert not result.applied
    assert result.image is clean


def test_rotation_never_crops_the_label_away(clean: np.ndarray) -> None:
    """The canvas expands. On a back label the bottom corners are where the government
    warning ends, and a corner trimmed there becomes a false Missing."""
    rotated = deskew.rotate_upright(clean, 20.0)
    assert rotated.shape[0] > clean.shape[0] and rotated.shape[1] > clean.shape[1]
    assert _ink(rotated) >= _ink(clean) * 0.97


def test_rotation_fills_with_the_border_colour_not_black(clean: np.ndarray) -> None:
    """Black fill would tank the exposure score of a well-lit photo and invent a hard
    edge for the skew estimator to lock onto."""
    corner = deskew.rotate_upright(clean, 20.0)[2, 2]
    assert corner.mean() > 200


# --- perspective ------------------------------------------------------------------------

@pytest.mark.tc("TC-11")
@pytest.mark.parametrize("degrees", [15.0, 30.0, 45.0])
def test_an_off_axis_photo_is_rectified(clean: np.ndarray, degrees: float) -> None:
    result = deskew.correct(degrade.on_surface(clean, degrees=degrees))
    assert result.perspective_applied


@pytest.mark.tc("TC-11")
@pytest.mark.parametrize("degrees", [15.0, 30.0, 45.0])
def test_rectification_recovers_the_label_shape(clean: np.ndarray, degrees: float) -> None:
    """A rectified label should be close to the aspect ratio it was drawn at."""
    result = deskew.correct(degrade.on_surface(clean, degrees=degrees))
    aspect = result.image.shape[0] / result.image.shape[1]
    assert aspect == pytest.approx(clean.shape[0] / clean.shape[1], rel=0.25)


@pytest.mark.tc("TC-11")
def test_rectification_drops_the_surface_around_the_label(clean: np.ndarray) -> None:
    """The dark desk the label was photographed on must not survive into extraction.

    Measured as a share of the frame rather than by sampling a corner: the detected
    boundary sits a few pixels outside the label because the edge map is dilated, so a
    thin rim of surface legitimately survives. A rim is not the problem; half the picture
    being desk is.
    """
    photo = degrade.on_surface(clean, degrees=30.0)
    result = deskew.correct(photo)

    def surface_fraction(image: np.ndarray) -> float:
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        return float((gray < 110).mean())

    assert surface_fraction(photo) > 0.4
    assert surface_fraction(result.image) < 0.1


@pytest.mark.tc("TC-11")
@pytest.mark.parametrize("degrees", [0.0, 15.0, 30.0, 45.0])
def test_rectification_keeps_the_text(clean: np.ndarray, degrees: float) -> None:
    """The whole point of correcting geometry is to read the label afterwards."""
    result = deskew.correct(degrade.on_surface(clean, degrees=degrees))
    assert _ink(result.image) >= _ink(clean) * 0.75


def test_no_boundary_means_no_rectification(clean: np.ndarray) -> None:
    """A label photographed edge to edge has no corners. None is the honest answer."""
    assert deskew.find_label_quad(clean) is None


def test_a_borderless_label_passes_through_untouched(clean: np.ndarray) -> None:
    result = deskew.correct(clean)
    assert not result.perspective_applied
    assert "no label boundary" in result.note


# --- the crop guard (the LP-326 failure, refused here) -------------------------------------

def test_a_quad_that_would_slice_off_text_is_refused(clean: np.ndarray) -> None:
    """A crop that loses the bottom of a back label loses the government warning, after
    which the pipeline reports Missing on a compliant label — a false finding this
    pipeline manufactured itself."""
    quad = np.float32([[0, 0], [clean.shape[1], 0], [clean.shape[1], 700], [0, 700]])
    assert deskew.ink_outside(clean, quad) > 0.1


def test_a_quad_containing_everything_discards_nothing(clean: np.ndarray) -> None:
    h, w = clean.shape[:2]
    quad = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    assert deskew.ink_outside(clean, quad) == pytest.approx(0.0, abs=0.01)


def test_correct_refuses_a_cropping_rectification(clean: np.ndarray) -> None:
    """Composited so the detected boundary sits inside the text: the pass must decline."""
    photo = degrade.on_surface(clean, degrees=0.0, margin=0.15)
    band = photo.copy()
    # Paint a high-contrast rectangle over the upper half — a plausible false boundary.
    cv2.rectangle(band, (60, 60), (band.shape[1] - 60, band.shape[0] // 2), (10, 10, 10), 6)
    result = deskew.correct(band)
    assert _ink(result.image) >= _ink(photo) * 0.75


# --- LP-201: curved surfaces ------------------------------------------------------------------

def test_a_cylinder_warp_stays_readable(clean: np.ndarray) -> None:
    """A label wrapped around a bottle is not rectifiable by a four-point transform — the
    distortion is not projective. The honest outcome is that it stays legible, not that
    it gets flattened."""
    warped = degrade.cylinder(clean)
    result = deskew.correct(warped)
    assert quality.assess(result.image).verdict != "hopeless"


def test_a_cylinder_warp_is_not_pretended_to_be_flattened(clean: np.ndarray) -> None:
    """No claim of a correction that did not happen. `applied` is the audit trail."""
    result = deskew.correct(degrade.cylinder(clean))
    assert not result.perspective_applied


@pytest.mark.tc("TC-11")
def test_a_cylinder_photographed_off_axis_is_still_worth_reading(clean: np.ndarray) -> None:
    photo = degrade.on_surface(degrade.cylinder(clean), degrees=20.0)
    assert quality.assess(deskew.correct(photo).image).verdict != "hopeless"


# --- never make it worse -----------------------------------------------------------------------

@pytest.mark.parametrize("degrees", [0.0, 6.0, 15.0, 30.0])
def test_correction_never_lowers_the_blur_score_materially(
    clean: np.ndarray, degrees: float
) -> None:
    photo = degrade.on_surface(clean, degrees=degrees)
    result = deskew.correct(photo)
    assert quality.blur_score(result.image) >= quality.blur_score(photo) - 0.1


def test_correction_of_noise_does_not_claim_success() -> None:
    """Random pixels have no text lines and no label boundary. Anything this returns as
    'corrected' would be an invented correction."""
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 255, size=(600, 800, 3), dtype=np.uint8)
    result = deskew.correct(noise)
    assert not result.perspective_applied


def test_the_pass_is_deterministic(clean: np.ndarray) -> None:
    photo = degrade.on_surface(clean, degrees=25.0)
    assert np.array_equal(deskew.correct(photo).image, deskew.correct(photo).image)


def test_every_result_explains_itself(clean: np.ndarray) -> None:
    """The note is what ends up in the log when someone asks why a box moved."""
    for image in (clean, degrade.rotate(clean, 9.0), degrade.on_surface(clean, degrees=30.0)):
        assert deskew.correct(image).note


# --- corner ordering -----------------------------------------------------------------------------

def test_corners_are_ordered_regardless_of_input_order() -> None:
    quad = np.float32([[10, 10], [110, 20], [100, 210], [5, 200]])
    expected = deskew.order_corners(quad)
    for roll in range(4):
        assert np.allclose(deskew.order_corners(np.roll(quad, roll, axis=0)), expected)


def test_corner_ordering_survives_strong_foreshortening() -> None:
    """Sorting by angle around the centroid breaks here; sums and differences do not."""
    quad = np.float32([[100, 0], [400, 40], [400, 300], [40, 340]])
    ordered = deskew.order_corners(quad)
    assert ordered[0][1] <= ordered[3][1]   # top-left above bottom-left
    assert ordered[0][0] <= ordered[1][0]   # top-left left of top-right
