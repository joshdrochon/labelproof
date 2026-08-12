"""Image quality scoring and the pre-gate (IMG-4, LP-321).

Deterministic, no model call, tens of milliseconds. Four measurements plus a resolution
check, each normalized to 0..1 where 1 is best so they read and compose consistently.

**The pre-gate is the point.** An image that is hopeless gets a plain-language retake
reason and *zero* model calls. That path can only ever spend less, and it can never
produce a false pass, because its outcome is "we did not verify this" rather than "we
verified this and it was fine".

**What the pre-gate does not do:** it catches illegible images, not wrong-subject ones. A
photograph of a cat is sharp, well exposed, and perfectly scored — TC-15 needs the model.

**Quality is also judged per region, not only per image (IMG-5, LP-192).** A whole-image
score answers "is this a nice photograph", which is not the question. A label can be
tack-sharp everywhere except the flash reflection sitting across the government warning,
and a single global number reports that image as fine. `assess_region` scores one
rectangle of the frame on the same scale, so the warning can come back Unreadable while
the brand two inches above it comes back verified — which is TC-12, and is the difference
between an honest result and a false pass.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import cv2
import numpy as np

from api.models import BoundingBox, ImageQuality
from api.rules import thresholds as T

#: Orientations the blur measure samples, in [0, π). Eight, at 22.5°, because a smear is
#: only visible in the direction it acts along and the worst case has to land near a
#: sample. Measured on a 51-pixel line PSF swept 0–90°: two axes leaves a worst case of
#: 131, four gives 67, eight gives 58, twelve also gives 58. Eight is where it stops
#: paying. θ and θ+π give the same variance, so half the circle is the whole story.
_ORIENTATIONS: tuple[float, ...] = tuple(np.pi * k / 8 for k in range(8))

#: Gaussian σ applied before differentiating, so broadband sensor noise is not counted as
#: detail. Without it, an out-of-focus photo at radius 16 scored 56 clean and 556 with
#: ordinary high-ISO noise — a tenfold inflation straight through the pre-gate. At σ=2 the
#: pair is 42 and 40.
_NOISE_PRESMOOTH_SIGMA = 2.0


def _to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)


def blur_score(image: np.ndarray, mask: np.ndarray | None = None) -> float:
    """Edge energy in the worst direction, on a log scale.

    **Log rather than linear**, because the measure spans four orders of magnitude on real
    content. A linear normalization puts everything below "perfectly sharp" into the same
    bucket near zero, which is how the first version of this scored every degraded image
    identically.

    **Contrast is normalized away first.** Gradient magnitudes scale linearly with
    contrast and variance with its square, so a merely *dark* image measures as blurry —
    and the retake reason then tells the agent to hold the camera steady when the real
    problem was the lighting. Stretching to full range first keeps "too dark" and "too
    blurry" as the separate problems they are.

    **The worst of eight directions, not two axes and not an isotropic operator.** Blur
    destroys detail along the direction it acts in, so the measure has to be directional.
    A Laplacian sums every direction at once and misses that entirely: a 25-pixel motion
    smear scored the same as a defocus of radius 2, which is merely soft.

    Two axes is not enough either, and the reason is worth stating because it is not
    obvious. `min(var(gx), var(gy))` only sees a smear that happens to lie along an image
    axis. At 45° both Sobel axes lose the *same* energy, neither is the destroyed one, and
    the score plateaus no matter how long the smear gets — measured on a true line PSF, a
    51-pixel smear at 45° scored 422 against a hopeless floor of 120, while the same smear
    at 0° scored 49. An unreadable label was passed to the model with "the photo is
    slightly soft". Fixing the isotropic blind spot by going to two axes had moved it
    rather than removed it.

    Eight orientations at 22.5° closes it: worst case over every angle drops from 131 to
    58. Twelve orientations measured no better than eight, so eight is where this stops
    paying. The directional derivative along θ is `gx·cosθ + gy·sinθ`, so all eight come
    from one pair of Sobel passes.

    **Pre-smoothed at σ=2 before differentiating**, which is what stops sensor noise
    reading as detail. Noise is broadband, so it lifts every direction at once and the
    minimum with it: an out-of-focus photo at radius 16 scored 56 clean and 556 with
    ordinary high-ISO noise on top — a tenfold inflation, straight through the pre-gate.
    After pre-smoothing the same pair is 42 and 40. The cost is range at the sharp end
    (a clean label drops from 10825 to 1255), which the log scale absorbs.

    `mask` restricts the measurement to part of the frame, and exists because variance is
    diluted by flat area. A rotation that expands the canvas adds a wide band of uniform
    fill, which drops the score even though not one pixel of text got softer — measured
    as a 0.09 loss for a rotation a masked measurement scores at 0.02. Without it the
    correction pass kept reverting rotations that were fine.
    """
    gray = _to_gray(image).astype(np.float32)

    selection: np.ndarray | None = None
    if mask is not None:
        # Eroded by enough to clear both the pre-smoothing kernel and the Sobel, so the
        # step between real content and fill is never read as detail.
        eroded = cv2.erode((mask > 0).astype(np.uint8), np.ones((11, 11), np.uint8))
        selection = eroded > 0
        if not selection.any():
            return 0.0
        low, high = float(gray[selection].min()), float(gray[selection].max())
    else:
        low, high = float(gray.min()), float(gray.max())

    if high - low > 1.0:
        gray = np.clip((gray - low) * (255.0 / (high - low)), 0.0, 255.0)

    gray = np.asarray(
        cv2.GaussianBlur(gray, (0, 0), _NOISE_PRESMOOTH_SIGMA), dtype=np.float32
    )
    gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    if selection is not None:
        gx, gy = gx[selection], gy[selection]

    variance = min(
        float((gx * np.cos(theta) + gy * np.sin(theta)).var())
        for theta in _ORIENTATIONS
    )

    if variance <= T.BLUR_HOPELESS_VARIANCE:
        return 0.0
    span = np.log10(T.SHARP_GRADIENT_VARIANCE / T.BLUR_HOPELESS_VARIANCE)
    return float(
        np.clip(np.log10(variance / T.BLUR_HOPELESS_VARIANCE) / span, 0.0, 1.0)
    )


def exposure_score(image: np.ndarray) -> float:
    """Penalise images too dark to read. Deliberately does NOT penalise brightness.

    Labels are mostly light — cream, white, foil — so a high mean luminance is normal
    rather than a defect. An earlier version treated mean above 225 as overexposed, which
    scored a perfectly good label as bad and, worse, made *dimming* an image improve its
    score.

    Blown-out highlights are real, but they are glare, and `glare_score` measures them by
    counting saturated pixels. Measuring them here too would double-count.
    """
    mean = float(_to_gray(image).mean())
    return float(np.clip(mean / T.EXPOSURE_FLOOR, 0.0, 1.0))


#: Luminance at or above which a pixel carries no detail. The score that *reports* glare
#: and the mask that *acts* on it (`preprocess.glare_mask`) have to mean the same thing, so
#: this is the one definition and preprocess imports it. Two copies of the number would let
#: the report and the mask disagree about which pixels are gone.
BLOWN_LEVEL = 250


def glare_score(image: np.ndarray) -> float:
    """Fraction of near-saturated pixels, inverted.

    Glare on glass is a cluster of blown-out pixels. Measured as a share of the frame, so
    a small specular highlight barely registers while a flash across the label does.
    """
    gray = _to_gray(image)
    blown = float((gray >= BLOWN_LEVEL).sum()) / gray.size
    return max(0.0, 1.0 - blown / T.GLARE_SATURATION_FRACTION)


def skew_degrees(image: np.ndarray) -> float:
    """Dominant text-line angle, in degrees off horizontal.

    Delegates to `deskew.estimate_skew` so the number reported to the agent and the number
    the correction pass acts on are the same number. Two implementations would drift, and
    a quality report that disagreed with what preprocessing actually did would be worse
    than no report at all.
    """
    from api.pipeline.deskew import estimate_skew

    return estimate_skew(image)


def assess(image: np.ndarray) -> ImageQuality:
    """Score one image and decide whether it is worth sending to a model."""
    blur = blur_score(image)
    exposure = exposure_score(image)
    glare = glare_score(image)
    skew = skew_degrees(image)
    long_edge = max(image.shape[0], image.shape[1])
    resolution_ok = long_edge >= T.MIN_LONG_EDGE_PX

    worst = min(blur, exposure, glare)

    if worst < T.HOPELESS:
        verdict, reason = "hopeless", _retake_reason(blur, exposure, glare)
    elif worst < T.DEGRADED or not resolution_ok:
        verdict, reason = "degraded", _degraded_reason(blur, exposure, glare, resolution_ok)
    else:
        verdict, reason = "ok", None

    return ImageQuality(
        blur=round(blur, 3),
        exposure=round(exposure, 3),
        glare=round(glare, 3),
        skew_deg=skew,
        resolution_ok=resolution_ok,
        verdict=verdict,
        reason=reason,
    )


def should_skip_extraction(quality: ImageQuality) -> bool:
    """LP-321 — is this image hopeless enough to skip the model entirely?"""
    return quality.verdict == "hopeless"


# --------------------------------------------------------------------------------------
# Per-region readability (IMG-5, LP-192)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RegionQuality:
    """How readable one rectangle of the frame is.

    `verdict` carries a fourth value the whole-image assessment has no use for: `blank`,
    meaning the region is *certainly* visible and there is *certainly* nothing printed in
    it. That distinction is the difference between Missing and Unreadable, and collapsing
    the two would either invent a legibility problem or hide one.

    `blank` is the only verdict here that is legible without anything being readable, so
    it is deliberately hard to earn — a region merely too faint to resolve is `hopeless`,
    not `blank`. Faint print and noisy blank stock are not distinguishable by contrast
    alone, and the band where they overlap is exactly where WARN-5 prominence violations
    live.
    """

    blur: float
    exposure: float
    glare: float
    has_content: bool
    verdict: str  # "ok" | "degraded" | "hopeless" | "blank"
    reason: str | None = None

    @property
    def legible(self) -> bool:
        """False only for `hopeless`. Blank is legible — there is just nothing to read."""
        return self.verdict != "hopeless"


def crop(image: np.ndarray, box: BoundingBox) -> np.ndarray:
    """The pixels inside a normalized 0..1 box, clamped to at least one pixel.

    Normalized against the *preprocessed* image, per the build spec — deskew changes
    geometry, so a box drawn over the original upload drifts.
    """
    h, w = image.shape[:2]
    y0 = min(int(box.y0 * h), max(h - 1, 0))
    x0 = min(int(box.x0 * w), max(w - 1, 0))
    y1 = min(max(int(box.y1 * h), y0 + 1), h)
    x1 = min(max(int(box.x1 * w), x0 + 1), w)
    return image[y0:y1, x0:x1]


#: Below this on either side a region carries no measurable signal. A box that small is a
#: broken box, not a reading.
_MIN_REGION_PX = 8


#: Relative contrast at or above which a region certainly carries print. Normal label text
#: sits above 0.9 even at a tenth of normal exposure; a warning rendered at 15% of full
#: contrast — already a prominence violation — measures 0.141.
_CONTENT_CONTRAST_RATIO = 0.15

#: …and at or below which it is certainly bare stock. Clean label stock measures 0.000.
_BLANK_CONTRAST_RATIO = 0.04

# Between the two is a band nothing can resolve, and pretending otherwise was a real bug.
# Measured on the generator: a warning printed at 12% contrast reads 0.113, and blank stock
# under σ=16 sensor noise reads 0.106. Those overlap. There is no threshold that separates
# "very faint print" from "empty paper photographed badly", so the honest outcome in that
# band is "could not tell", and "could not tell" has to fail closed — it is exactly where
# WARN-5 prominence violations live, and calling one of those blank would report a buried
# warning as Missing while marking the field legible on the way past.


def _content_ratio(region: np.ndarray) -> float:
    """Is anything printed here, as opposed to bare label stock?

    Measured as contrast *relative* to the region's own brightness, over a median-filtered
    copy. Three rejected alternatives, each of which gets a real case wrong:

    * **Edge density** calls text blurred past legibility blank, reporting a genuine TC-14
      failure as an empty part of the label.
    * **Absolute range** calls a dimly lit line of text blank.
    * **Percentiles** call `750 mL` blank, because a short line across a wide box is a
      couple of percent of the pixels and the 2nd percentile is still bare stock.

    The median filter is what makes min-to-max safe to use: it removes the isolated hot
    pixels a real sensor produces without touching a letter stroke.
    """
    gray = _to_gray(region)
    if gray.size == 0:
        return 0.0
    smoothed = cv2.medianBlur(gray, 3) if min(gray.shape[:2]) >= 3 else gray
    low, high = float(smoothed.min()), float(smoothed.max())
    return (high - low) / max(high, 1.0)


def assess_region(
    image: np.ndarray,
    box: BoundingBox,
    whole: ImageQuality | None = None,
) -> RegionQuality:
    """Score one region of the label on the same 0..1 scale the whole image uses.

    Blur is only allowed to condemn a region that has something printed in it. Gradient
    variance over bare label stock is legitimately near zero, and calling that "too blurry
    to read" would flag every field whose evidence box happens to include a margin — a
    wall of false flags, which is its own adoption failure (UX-7).

    `whole` is the assessment of the entire image, and passing it closes a gap the two
    measurements had between them. Contrast is stretched per region and variance is
    diluted by blank area, so a dense band of text scores better on its own than the
    picture containing it: on a radius-16 defocus the whole image scored 0.000 and hopeless
    while the warning region of that same image scored 0.307 and legible. The global gate
    said nobody could read this photograph and the region gate — the one that decides
    Unreadable — said the warning was fine. No region of an image the pre-gate rejected is
    legible, and saying so here is cheaper than reconciling two numbers later.
    """
    region = crop(image, box)
    if min(region.shape[:2]) < _MIN_REGION_PX:
        # Fails closed. A region too small to measure is not evidence that it is
        # readable, and this set is what forces a field to Unreadable.
        return RegionQuality(
            blur=0.0,
            exposure=0.0,
            glare=0.0,
            has_content=False,
            verdict="hopeless",
            reason="This part of the label could not be measured.",
        )

    blur = blur_score(region)
    exposure = exposure_score(region)
    glare = glare_score(region)
    ratio = _content_ratio(region)
    content = ratio >= _CONTENT_CONTRAST_RATIO
    certainly_blank = ratio <= _BLANK_CONTRAST_RATIO

    applicable = [exposure, glare] + ([blur] if content else [])
    worst = min(applicable)

    if whole is not None and whole.verdict == "hopeless":
        verdict = "hopeless"
        reason = whole.reason or "This image could not be read."
    elif worst < T.HOPELESS:
        verdict = "hopeless"
        reason = _region_reason(blur if content else 1.0, exposure, glare)
    elif certainly_blank:
        verdict, reason = "blank", "Nothing is printed in this part of the label."
    elif not content:
        # The unresolvable band. Faint print and noisy blank stock overlap here, so the
        # only honest answer is that we could not tell — which must not read as legible.
        verdict = "hopeless"
        reason = (
            "This part of the label is too faint to tell print from blank paper."
        )
    elif worst < T.DEGRADED:
        verdict = "degraded"
        reason = "This part of the label is hard to read."
    else:
        verdict, reason = "ok", None

    return RegionQuality(
        blur=round(blur, 3),
        exposure=round(exposure, 3),
        glare=round(glare, 3),
        has_content=content,
        verdict=verdict,
        reason=reason,
    )


def assess_regions[K](
    image: np.ndarray, boxes: Mapping[K, BoundingBox]
) -> dict[K, RegionQuality]:
    """Score several named regions of one image.

    The whole-image assessment is computed once and shared, so every region is judged
    against the same picture rather than each one re-deriving it.
    """
    whole = assess(image)
    return {key: assess_region(image, box, whole) for key, box in boxes.items()}


def illegible_regions[K](image: np.ndarray, boxes: Mapping[K, BoundingBox]) -> set[K]:
    """The regions nobody could read — the fields that must come back Unreadable.

    This is the mechanism behind TC-12: glare over the warning statement puts exactly one
    key in this set, the extractor is told that field is not legible, and the brand on the
    dry half of the label is verified normally.
    """
    return {
        key
        for key, assessment in assess_regions(image, boxes).items()
        if not assessment.legible
    }


def _region_reason(blur: float, exposure: float, glare: float) -> str:
    worst = min(blur, exposure, glare)
    if worst == glare:
        return "Glare is covering this part of the label."
    if worst == exposure:
        return "This part of the label is too dark to read."
    return "This part of the label is too blurry to read."


def _retake_reason(blur: float, exposure: float, glare: float) -> str:
    """Plain language, in the agents' own workflow verb (IMG-4, UX-6).

    Names the single worst problem rather than listing everything. An agent needs to know
    what to ask for, not a diagnostic report.
    """
    worst = min(blur, exposure, glare)
    if worst == glare:
        return (
            "Glare is covering too much of the label to read it. Retake the photo "
            "without flash, or request a new image."
        )
    if worst == exposure:
        return (
            "The photo is too dark to read the label. Retake it in better light, or "
            "request a new image."
        )
    return (
        "The photo is too blurry to read the label. Retake it holding the camera "
        "steady, or request a new image."
    )


def _degraded_reason(blur: float, exposure: float, glare: float, resolution_ok: bool) -> str:
    if not resolution_ok:
        return (
            "This image is lower resolution than recommended. Small text such as the "
            "warning statement may not be verifiable."
        )
    worst = min(blur, exposure, glare)
    if worst == glare:
        return "Some glare on the label. Parts of it may not be readable."
    if worst == exposure:
        return "The photo is dim. Parts of the label may not be readable."
    return "The photo is slightly soft. Parts of the label may not be readable."
