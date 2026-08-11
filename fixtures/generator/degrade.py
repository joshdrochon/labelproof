"""Optical degradations applied to rendered labels.

Covers TC-11 through TC-14 — the conditions Jenny described: weird angles, bad lighting,
glare on the bottle. Applied in code rather than photographed, so every degradation is
reproducible byte for byte, which regression tests require and a photograph cannot give.

**The honest limitation:** these simulate optics, not physics. Real specular highlights on
curved glass and real lens blur differ from a Gaussian and an overlay. That gap is why
Tier B exists (BUILD.md §5), and it belongs in the limitations list rather than being
papered over.
"""

from __future__ import annotations

import cv2
import numpy as np


def _rng(seed: int) -> np.random.Generator:
    """Seeded so every degradation is reproducible (LP-123)."""
    return np.random.default_rng(seed)


def rotate(image: np.ndarray, degrees: float) -> np.ndarray:
    """In-plane rotation — a photo taken off-square. TC-11's mild form."""
    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    return cv2.warpAffine(
        image, matrix, (w, h), flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def perspective(image: np.ndarray, degrees: float) -> np.ndarray:
    """Off-axis perspective — the camera to one side of the bottle (TC-11).

    Foreshortens one edge proportionally to the angle, which is what actually happens
    when you photograph a flat label from an angle.
    """
    h, w = image.shape[:2]
    shift = np.tan(np.radians(min(abs(degrees), 60.0))) * h * 0.25
    source = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    if degrees >= 0:
        dest = np.float32([[shift, 0], [w, 0], [w, h], [shift * 0.4, h]])
    else:
        dest = np.float32([[0, 0], [w - shift, 0], [w - shift * 0.4, h], [0, h]])
    matrix = cv2.getPerspectiveTransform(source, dest)
    return cv2.warpPerspective(
        image, matrix, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    )


def cylinder(image: np.ndarray, strength: float = 0.35) -> np.ndarray:
    """Wrap the label around a bottle (LP-201).

    Horizontal compression toward the edges, which is what a cylindrical surface does to
    flat artwork. The centre stays legible while the edges crowd — exactly the failure
    mode on a real bottle.
    """
    h, w = image.shape[:2]
    xs = np.arange(w, dtype=np.float32)
    normalized = (xs - w / 2) / (w / 2)
    warped = np.sin(normalized * np.pi / 2) * (w / 2) * (1 - strength) + w / 2
    map_x = np.tile(warped, (h, 1)).astype(np.float32)
    map_y = np.tile(np.arange(h, dtype=np.float32)[:, None], (1, w))
    return cv2.remap(
        image, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    )


def blur(image: np.ndarray, radius: float) -> np.ndarray:
    """Out-of-focus. Radius above ~9 is TC-14 territory — genuinely unreadable."""
    kernel = max(3, int(radius) * 2 + 1)
    return cv2.GaussianBlur(image, (kernel, kernel), radius)


def dim(image: np.ndarray, factor: float = 0.35) -> np.ndarray:
    """Underexposed but recoverable (TC-13). Scales luminance without crushing to black."""
    return np.clip(image.astype(np.float32) * factor, 0, 255).astype(np.uint8)


def glare(
    image: np.ndarray,
    *,
    centre: tuple[float, float] = (0.5, 0.75),
    radius: float = 0.28,
    intensity: float = 1.0,
    seed: int = 7,
) -> np.ndarray:
    """A specular blow-out over part of the label (TC-12).

    Defaults put it over the lower portion, where the warning statement sits — the case
    that must produce Unreadable for the warning while other fields stay verified.
    """
    h, w = image.shape[:2]
    cy, cx = int(h * centre[1]), int(w * centre[0])
    radius_px = radius * max(h, w)

    ys, xs = np.ogrid[:h, :w]
    distance = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
    falloff = np.clip(1.0 - (distance / radius_px) ** 2, 0.0, 1.0) * intensity

    # A little noise so the patch is not a perfect analytic disc.
    falloff = falloff * (1.0 + _rng(seed).normal(0, 0.03, falloff.shape))
    falloff = np.clip(falloff, 0.0, 1.0)[..., None]

    white = np.full_like(image, 255, dtype=np.float32)
    blended = image.astype(np.float32) * (1 - falloff) + white * falloff
    return np.clip(blended, 0, 255).astype(np.uint8)


#: Named degradations per canonical test case, so a fixture name maps to one transform.
PRESETS: dict[str, str] = {
    "tc11_angle_15": "perspective at 15 degrees",
    "tc11_angle_30": "perspective at 30 degrees",
    "tc11_angle_45": "perspective at 45 degrees",
    "tc12_glare_warning": "specular blow-out over the warning statement",
    "tc13_dim": "underexposed but recoverable",
    "tc14_blur_hopeless": "out of focus past legibility",
    "lp201_cylinder": "wrapped around a bottle",
}


def apply_preset(image: np.ndarray, preset: str) -> np.ndarray:
    match preset:
        case "tc11_angle_15":
            return perspective(image, 15.0)
        case "tc11_angle_30":
            return perspective(image, 30.0)
        case "tc11_angle_45":
            return perspective(image, 45.0)
        case "tc12_glare_warning":
            return glare(image)
        case "tc13_dim":
            return dim(image)
        case "tc14_blur_hopeless":
            return blur(image, 12.0)
        case "lp201_cylinder":
            return cylinder(image)
        case _:
            raise KeyError(f"unknown degradation preset {preset!r}")
