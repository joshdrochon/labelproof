"""Image quality scoring and the pre-gate (IMG-4, LP-321).

Deterministic, no model call, tens of milliseconds. Four measurements plus a resolution
check, each normalized to 0..1 where 1 is best so they read and compose consistently.

**The pre-gate is the point.** An image that is hopeless gets a plain-language retake
reason and *zero* model calls. That path can only ever spend less, and it can never
produce a false pass, because its outcome is "we did not verify this" rather than "we
verified this and it was fine".

**What the pre-gate does not do:** it catches illegible images, not wrong-subject ones. A
photograph of a cat is sharp, well exposed, and perfectly scored — TC-15 needs the model.
"""

from __future__ import annotations

import cv2
import numpy as np

from api.models import ImageQuality
from api.rules import thresholds as T


def _to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)


def blur_score(image: np.ndarray) -> float:
    """Laplacian variance on a log scale.

    Log rather than linear because the measure spans four orders of magnitude on real
    content — a sharp label renders around 1400, the same label at Gaussian radius 2
    around 37, and at radius 12 under 1. A linear normalization puts everything below
    "perfectly sharp" into the same bucket near zero.

    Contrast is normalized away first. Laplacian values scale linearly with contrast, so
    variance scales with its square — meaning a merely *dark* image measures as blurry.
    Stretching to full range before measuring decouples the two, so "too dark" and "too
    blurry" are reported as the separate problems they are, and each retake reason names
    what the agent actually needs to fix.
    """
    gray = _to_gray(image).astype(np.float32)
    low, high = float(gray.min()), float(gray.max())
    if high - low > 1.0:
        gray = (gray - low) * (255.0 / (high - low))
    variance = float(cv2.Laplacian(gray.astype(np.uint8), cv2.CV_64F).var())
    if variance <= T.BLUR_HOPELESS_VARIANCE:
        return 0.0
    span = np.log10(T.SHARP_LAPLACIAN_VARIANCE / T.BLUR_HOPELESS_VARIANCE)
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


def glare_score(image: np.ndarray) -> float:
    """Fraction of near-saturated pixels, inverted.

    Glare on glass is a cluster of blown-out pixels. Measured as a share of the frame, so
    a small specular highlight barely registers while a flash across the label does.
    """
    gray = _to_gray(image)
    blown = float((gray >= 250).sum()) / gray.size
    return max(0.0, 1.0 - blown / T.GLARE_SATURATION_FRACTION)


def skew_degrees(image: np.ndarray) -> float:
    """Dominant text-line angle, in degrees off horizontal.

    Hough over detected edges. Returns 0.0 when no dominant orientation is found, which
    is the honest answer for an image with no strong lines — not a claim of squareness.
    """
    gray = _to_gray(image)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLines(edges, 1, np.pi / 180, threshold=200)
    if lines is None:
        return 0.0

    angles: list[float] = []
    for line in lines[:60]:
        theta = float(line[0][1])
        degrees = np.degrees(theta) - 90.0
        if -45.0 <= degrees <= 45.0:
            angles.append(degrees)
    return round(float(np.median(angles)), 2) if angles else 0.0


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
