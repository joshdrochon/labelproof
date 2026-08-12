"""Geometric correction — rotation and perspective (IMG-1, IMG-6, LP-189, LP-201).

Jenny's words were "photographed at weird angles". Two different defects hide in that
phrase and they need different machinery:

* **In-plane rotation.** The camera was square to the label but held crooked. Text lines
  are straight and parallel, just tilted. Measured with Hough and undone with a rotation.
* **Perspective.** The camera was off to one side. Text lines are no longer parallel and
  no rotation fixes it — the label has to be rectified back to a rectangle from its four
  corners.

**Three rules this module will not break.**

1. **Never crop content away.** Rotation expands the canvas rather than trimming corners,
   and a perspective rectification is refused outright when any ink sits outside the
   detected quadrilateral. A crop that slices the bottom off a back label removes the
   government warning, and the pipeline would then report it Missing — a false finding
   manufactured by our own preprocessing. That is worse than leaving the image crooked.
2. **Never guess a correction.** No dominant line orientation means no rotation, and no
   convincing quadrilateral means no rectification. A label photographed edge-to-edge has
   no visible boundary, so `find_label_quad` returns None and the honest outcome is to
   leave the geometry alone.
3. **Verify, then keep or revert.** Every correction is measured after the fact and
   discarded if it did not actually improve the image. A deskew that made things worse is
   a plausible failure — Hough can lock onto a decorative rule rather than the text — and
   the guard costs one extra measurement.

Bounding boxes are normalized against the *preprocessed* image (pinned build decision), which is
what makes it safe for this module to change geometry at all: the UI shows the same image
the evidence boxes were drawn on.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from api.models import BoundingBox
from api.rules import thresholds as T

#: Hough accumulator votes required, as a fraction of the image's long edge. Relative
#: rather than absolute because a fixed threshold silently stops finding lines on a small
#: image and finds noise on a large one — the same photo at two resolutions would get two
#: different answers.
_HOUGH_VOTE_FRACTION = 0.14

#: Candidate lines required before `estimate_skew` will claim an angle at all.
#:
#: Three real photographs put this here. A straight-on shot of a wine back label returned
#: exactly -45.00 degrees, and so did a cropped Bacardi label, and a good Fireball photo
#: returned 34.0. In each case the estimator had found a handful of lines, none of them
#: text, and taken their median as if it were a measurement. One or two survivors is not a
#: dominant orientation; it is what is left after the filter, and reporting it as an angle
#: told the agent a square photograph was crooked.
_MIN_SKEW_CANDIDATES = 8

#: Maximum spread (half the 10th-to-90th-percentile range) the candidates may show before
#: the estimate is discarded as noise. Text lines on one label agree closely; a scatter of
#: bottle edges, shelf lines and glare boundaries does not.
_MAX_SKEW_SPREAD_DEG = 12.0

#: Readings this close to the filter boundary are saturation, not measurement. A label
#: whose text genuinely runs at 45 degrees is not a case this product has; a median that
#: lands on the boundary is the estimator running out of road.
_SKEW_BOUNDARY_DEG = 44.0

#: A detected quadrilateral must cover at least this share of the frame to be believed as
#: the label. Below it we are almost certainly looking at a decorative box or a shadow,
#: and rectifying to that would destroy the geometry rather than fix it.
_MIN_QUAD_AREA_FRACTION = 0.25

#: …and at most this share. A quadrilateral that *is* the frame is not a boundary anyone
#: found — it is what contour detection returns when the whole image is busy, which is
#: exactly what a photograph of something that is not a label looks like. Rectifying to it
#: is a no-op that would nonetheless be reported as a correction we made.
_MAX_QUAD_AREA_FRACTION = 0.95

#: Share of the frame's detail allowed to fall outside the detected quadrilateral before
#: the rectification is refused. Not zero: the approximated quad cuts corners off the true
#: boundary, so a little of the label's own edge always lands outside. Anything above this
#: means real content sits outside the crop.
_MAX_INK_OUTSIDE_QUAD = 0.02

#: How much sharpness a correction may cost before it is judged a bad trade. Resampling
#: always softens slightly; a large drop means the transform smeared the text.
_MAX_BLUR_SCORE_LOSS = 0.08


@dataclass(frozen=True)
class Deskewed:
    """The result of a geometric pass, and an honest account of what it did.

    `applied` is False whenever the pixels came through untouched, which is the common
    and correct outcome for a straight-on photograph.
    """

    image: np.ndarray
    rotation_deg: float = 0.0
    perspective_applied: bool = False
    note: str = "no correction needed"
    transform: np.ndarray | None = None
    """3x3 homography from input pixels to output pixels, or None when nothing moved.

    Carried because a correction that changes geometry silently invalidates every
    coordinate anyone already holds. the build spec handles that for evidence boxes by
    declaring them normalized against the *preprocessed* image — which works only as long
    as nothing ever needs to go the other way. Anything holding a box in the original
    frame needs this to follow the pixels, and without it the drift is invisible: the box
    still looks plausible, it just points at the wrong part of the label."""

    source_size: tuple[int, int] = (0, 0)
    """(height, width) of the image this pass was handed."""

    @property
    def applied(self) -> bool:
        return self.perspective_applied or self.rotation_deg != 0.0

    def map_box(self, box: BoundingBox) -> BoundingBox:
        """Carry a normalized box from the original frame into the corrected one.

        Returns the axis-aligned bounds of the transformed quadrilateral, which is the
        honest answer: a rectangle photographed off-axis is not a rectangle any more, and
        the smallest box that still contains all of it is the only one guaranteed not to
        clip evidence. It grows rather than shrinks, and growing is the safe direction —
        a box that lost part of the government warning would report it as legible on the
        strength of the half we could still see.
        """
        if self.transform is None or not self.source_size[0]:
            return box

        h, w = self.source_size
        corners = np.array(
            [
                [box.x0 * w, box.y0 * h, 1.0],
                [box.x1 * w, box.y0 * h, 1.0],
                [box.x1 * w, box.y1 * h, 1.0],
                [box.x0 * w, box.y1 * h, 1.0],
            ],
            dtype=np.float64,
        )
        moved = corners @ np.asarray(self.transform, dtype=np.float64).T
        denominator = np.where(np.abs(moved[:, 2]) < 1e-9, 1e-9, moved[:, 2])
        xs, ys = moved[:, 0] / denominator, moved[:, 1] / denominator

        out_h, out_w = self.image.shape[:2]
        return BoundingBox(
            x0=float(np.clip(xs.min() / out_w, 0.0, 1.0)),
            y0=float(np.clip(ys.min() / out_h, 0.0, 1.0)),
            x1=float(np.clip(xs.max() / out_w, 0.0, 1.0)),
            y1=float(np.clip(ys.max() / out_h, 0.0, 1.0)),
        )


def _to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)


# --------------------------------------------------------------------------------------
# In-plane rotation
# --------------------------------------------------------------------------------------


def estimate_skew(image: np.ndarray) -> float:
    """Dominant text-line angle in degrees off horizontal, or 0.0 when there is none.

    The median of the candidate line angles rather than the mean: a single decorative
    rule at 40° should not drag the estimate, and text lines outnumber ornament on a
    label. Returning 0.0 for "no dominant orientation" is the honest answer for an image
    with no strong lines — it is not a claim that the image is square.
    """
    gray = _to_gray(image)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    votes = max(60, int(max(image.shape[0], image.shape[1]) * _HOUGH_VOTE_FRACTION))
    lines = cv2.HoughLines(edges, 1, np.pi / 180, threshold=votes)
    if lines is None:
        return 0.0

    angles: list[float] = []
    for line in lines[:60]:
        theta = float(line[0][1])
        degrees = np.degrees(theta) - 90.0
        # Strictly inside the boundary. At exactly +/-45 the reading is the filter's edge
        # rather than the image's content — see _SKEW_BOUNDARY_DEG.
        if -_SKEW_BOUNDARY_DEG < degrees < _SKEW_BOUNDARY_DEG:
            angles.append(degrees)

    # Enough lines, and lines that agree. Either one alone is not enough: a handful of
    # agreeing bottle edges is still not text, and fifty scattered ones are still noise.
    if len(angles) < _MIN_SKEW_CANDIDATES:
        return 0.0
    low, high = np.percentile(angles, [10, 90])
    if (float(high) - float(low)) / 2.0 > _MAX_SKEW_SPREAD_DEG:
        return 0.0

    return round(float(np.median(angles)), 2)


def _border_colour(image: np.ndarray) -> tuple[int, ...]:
    """Median colour of the frame's outer ring, used to fill space a rotation opens up.

    Black fill would be a lie twice over: it tanks the exposure score of a perfectly well
    lit photo, and it invents a hard edge that the skew estimator can then lock onto.
    Replicating the border smears text outward instead, which invents structure. The
    median of the existing border is the least informative choice available.
    """
    ring = np.concatenate(
        [
            image[0, :].reshape(-1, image.shape[-1] if image.ndim == 3 else 1),
            image[-1, :].reshape(-1, image.shape[-1] if image.ndim == 3 else 1),
            image[:, 0].reshape(-1, image.shape[-1] if image.ndim == 3 else 1),
            image[:, -1].reshape(-1, image.shape[-1] if image.ndim == 3 else 1),
        ]
    )
    return tuple(int(v) for v in np.median(ring, axis=0))


def _rotation_matrix(image: np.ndarray, degrees: float) -> tuple[np.ndarray, int, int]:
    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_w = int(h * sin + w * cos)
    new_h = int(h * cos + w * sin)
    matrix[0, 2] += (new_w - w) / 2
    matrix[1, 2] += (new_h - h) / 2
    return matrix, new_w, new_h


def rotate_upright(image: np.ndarray, degrees: float) -> np.ndarray:
    """Rotate by `degrees`, expanding the canvas so no pixel leaves the frame.

    The expansion is the point. `cv2.warpAffine` at the original size trims the corners,
    and on a back label the bottom corners are where the warning statement ends.
    """
    matrix, new_w, new_h = _rotation_matrix(image, degrees)
    return cv2.warpAffine(
        image,
        matrix,
        (new_w, new_h),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=_border_colour(image),
    )


def content_mask(image: np.ndarray, degrees: float) -> np.ndarray:
    """Which pixels of a `rotate_upright` result came from the original image.

    Used to measure sharpness over the real content only. Without it the fill band an
    expanded rotation adds dilutes Laplacian variance and the correction looks like it
    softened text it never touched.
    """
    matrix, new_w, new_h = _rotation_matrix(image, degrees)
    solid = np.full(image.shape[:2], 255, np.uint8)
    return cv2.warpAffine(solid, matrix, (new_w, new_h), flags=cv2.INTER_NEAREST)


# --------------------------------------------------------------------------------------
# Perspective
# --------------------------------------------------------------------------------------


def order_corners(quad: np.ndarray) -> np.ndarray:
    """Order four points top-left, top-right, bottom-right, bottom-left.

    By coordinate sums and differences, which is orientation-independent — sorting by
    angle around the centroid breaks on a strongly foreshortened quadrilateral.
    """
    points = np.asarray(quad, dtype=np.float32).reshape(4, 2)
    total = points.sum(axis=1)
    diff = np.diff(points, axis=1).ravel()
    return np.array(
        [
            points[int(np.argmin(total))],
            points[int(np.argmin(diff))],
            points[int(np.argmax(total))],
            points[int(np.argmax(diff))],
        ],
        dtype=np.float32,
    )


def find_label_quad(image: np.ndarray) -> np.ndarray | None:
    """The label's four corners, or None when no convincing boundary exists.

    None is a normal, frequent answer. A label photographed edge to edge — every fixture
    this repo renders, and a scanned print proof — has no boundary against a background,
    so there is nothing to rectify and nothing to detect. Reporting a quadrilateral
    anyway would mean rectifying to the frame, which is a no-op at best and a crop at
    worst.
    """
    gray = _to_gray(image)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 40, 120)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    frame_area = float(image.shape[0] * image.shape[1])

    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:6]:
        area = cv2.contourArea(contour)
        if area < _MIN_QUAD_AREA_FRACTION * frame_area:
            break
        if area > _MAX_QUAD_AREA_FRACTION * frame_area:
            continue
        perimeter = cv2.arcLength(contour, True)
        for epsilon in (0.02, 0.03, 0.05):
            approx = cv2.approxPolyDP(contour, epsilon * perimeter, True)
            if len(approx) == 4 and cv2.isContourConvex(approx):
                return order_corners(approx.reshape(4, 2))
    return None


def ink_outside(image: np.ndarray, quad: np.ndarray) -> float:
    """Share of the image's *detail* that a crop to `quad` would discard.

    This is the guard that keeps LP-326's stated risk from coming true here. A
    rectification is a crop, and a crop that loses the bottom of a back label loses the
    government warning — after which the pipeline reports it Missing and the agent is
    told a false thing about a compliant label.

    Detail is measured as edge density, not as dark pixels. "Dark" is the wrong proxy:
    photograph a label on a dark desk and the desk is darker than the print, so a correct
    crop looks like it is throwing away most of the ink. Text is dense edges and a desk
    is flat, whatever its brightness.

    The quadrilateral is dilated before the test because the label's own boundary is a
    strong edge lying exactly on it, and counting that edge as discarded content would
    make every rectification look destructive.
    """
    gray = _to_gray(image)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 60, 160)
    total = int((edges > 0).sum())
    if total == 0:
        return 0.0

    mask = np.zeros(gray.shape, np.uint8)
    cv2.fillConvexPoly(mask, order_corners(quad).astype(np.int32), 255)
    margin = max(3, round(max(gray.shape) * 0.01))
    dilated = cv2.dilate(mask, np.ones((margin, margin), np.uint8))

    inside = int(((edges > 0) & (dilated > 0)).sum())
    return (total - inside) / total


def rectify(image: np.ndarray, quad: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Warp the quadrilateral back to a rectangle. Returns the image and its homography.

    Output size comes from the longest opposing edges, so the rectified label keeps the
    resolution of its least-foreshortened side rather than averaging detail away.
    """
    tl, tr, br, bl = order_corners(quad)
    width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    width, height = max(width, 1), max(height, 1)

    destination = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(order_corners(quad), destination)
    warped = cv2.warpPerspective(
        image, matrix, (width, height), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return warped, np.asarray(matrix, dtype=np.float64)


# --------------------------------------------------------------------------------------
# The pass
# --------------------------------------------------------------------------------------


def _sharpness(image: np.ndarray, mask: np.ndarray | None = None) -> float:
    from api.pipeline.quality import blur_score

    return blur_score(image, mask)


def correct(image: np.ndarray, *, allow_perspective: bool = True) -> Deskewed:
    """Straighten one image, or leave it exactly as it was and say why.

    Perspective first, then rotation. Rectifying from four corners already fixes the
    in-plane component, so doing rotation first would resample the image twice for one
    defect — and every resample costs sharpness on text that is already marginal.
    """
    working = image
    notes: list[str] = []
    perspective_applied = False
    transform: np.ndarray | None = None

    if allow_perspective:
        quad = find_label_quad(working)
        if quad is None:
            notes.append("no label boundary found — geometry left alone")
        elif ink_outside(working, quad) > _MAX_INK_OUTSIDE_QUAD:
            notes.append("label boundary would have cropped text — rectification refused")
        else:
            candidate, homography = rectify(working, quad)
            if _sharpness(candidate) >= _sharpness(working) - _MAX_BLUR_SCORE_LOSS:
                working, perspective_applied = candidate, True
                transform = homography
                notes.append("perspective rectified from the label boundary")
            else:
                notes.append("rectification would have blurred the text — reverted")

    rotation = 0.0
    skew = estimate_skew(working)
    if abs(skew) >= T.SKEW_CORRECTION_DEG:
        candidate = rotate_upright(working, skew)
        improved = abs(estimate_skew(candidate)) < abs(skew)
        # Measured over the rotated content only — the fill band an expanded rotation
        # adds is not text that got softer.
        kept_sharp = _sharpness(
            candidate, content_mask(working, skew)
        ) >= _sharpness(working) - _MAX_BLUR_SCORE_LOSS
        if improved and kept_sharp:
            affine, _, _ = _rotation_matrix(working, skew)
            spin = np.vstack([np.asarray(affine, dtype=np.float64), [0.0, 0.0, 1.0]])
            transform = spin if transform is None else spin @ transform
            working, rotation = candidate, skew
            notes.append(f"rotated {skew:+.2f}° to level the text")
        else:
            notes.append("rotation did not improve the image — reverted")

    return Deskewed(
        image=working,
        rotation_deg=rotation,
        perspective_applied=perspective_applied,
        note="; ".join(notes) if notes else "no correction needed",
        transform=transform,
        source_size=(image.shape[0], image.shape[1]),
    )
