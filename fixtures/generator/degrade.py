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

from fixtures.generator.layout import WARNING_BAND


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


def on_surface(
    image: np.ndarray,
    *,
    degrees: float = 0.0,
    margin: float = 0.15,
    surface: tuple[int, int, int] = (64, 62, 60),
) -> np.ndarray:
    """Photograph the label lying on a surface, optionally from off to one side.

    `perspective` warps the label but replicates the border, so the result fills the frame
    edge to edge and has *no visible boundary*. That is a faithful model of a scanned
    print proof and a useless one for testing perspective correction: with no boundary
    there are no corners, and rectification has nothing to work from.

    This composites the warped label onto a plain background instead, which is what a
    phone photo actually looks like and what LP-189's corner detection needs to exercise.
    The surface is a flat mid-dark grey rather than a texture — a textured desk would test
    contour detection against clutter, which is a different and much weaker claim than
    "the corners are found and the label is squared up".
    """
    h, w = image.shape[:2]
    pad_x, pad_y = int(w * margin), int(h * margin)
    canvas = np.full((h + 2 * pad_y, w + 2 * pad_x, 3), surface, dtype=np.uint8)
    height, width = canvas.shape[:2]

    shift = np.tan(np.radians(min(abs(degrees), 60.0))) * h * 0.25
    source = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    if degrees >= 0:
        dest = np.float32([[shift, 0], [w, 0], [w, h], [shift * 0.4, h]])
    else:
        dest = np.float32([[0, 0], [w - shift, 0], [w - shift * 0.4, h], [0, h]])
    dest = dest + np.float32([pad_x, pad_y])

    matrix = cv2.getPerspectiveTransform(source, dest)
    warped = cv2.warpPerspective(image, matrix, (width, height), flags=cv2.INTER_LINEAR)
    mask = cv2.warpPerspective(
        np.full((h, w), 255, np.uint8), matrix, (width, height), flags=cv2.INTER_NEAREST
    )

    out = canvas.copy()
    out[mask > 0] = warped[mask > 0]
    return out


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
    aspect: float = 1.0,
    intensity: float = 1.0,
    seed: int = 7,
) -> np.ndarray:
    """A specular blow-out over part of the label (TC-12).

    `aspect` stretches the patch horizontally. A flash reflection off a bottle is a band
    across the label, not a circle: it follows the curve of the glass, so it covers a few
    lines of text right across the width rather than a disc in the middle of one.
    """
    h, w = image.shape[:2]
    cy, cx = int(h * centre[1]), int(w * centre[0])
    radius_px = radius * max(h, w)

    ys, xs = np.ogrid[:h, :w]
    distance = np.sqrt(((xs - cx) / max(aspect, 1e-6)) ** 2 + (ys - cy) ** 2)
    falloff = np.clip(1.0 - (distance / radius_px) ** 2, 0.0, 1.0) * intensity

    # A little noise so the patch is not a perfect analytic disc.
    falloff = falloff * (1.0 + _rng(seed).normal(0, 0.03, falloff.shape))
    falloff = np.clip(falloff, 0.0, 1.0)[..., None]

    white = np.full_like(image, 255, dtype=np.float32)
    blended = image.astype(np.float32) * (1 - falloff) + white * falloff
    return np.clip(blended, 0, 255).astype(np.uint8)


def glare_over_warning(image: np.ndarray, *, seed: int = 7) -> np.ndarray:
    """A flash reflection sitting across the government warning and nothing else (TC-12).

    Sized to blow out the warning band right across the width while leaving the brand,
    class and alcohol content untouched. That separation is the entire test: the warning
    must come back Unreadable and every other field must still be verified. A patch that
    dimmed the whole label would prove only that a bad photo is a bad photo.
    """
    centre_y = (WARNING_BAND[0] + WARNING_BAND[1]) / 2
    return glare(
        image, centre=(0.5, centre_y), radius=0.045, aspect=7.0, intensity=1.0, seed=seed
    )


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
            return glare_over_warning(image)
        case "tc13_dim":
            return dim(image)
        case "tc14_blur_hopeless":
            return blur(image, 12.0)
        case "lp201_cylinder":
            return cylinder(image)
        case _:
            raise KeyError(f"unknown degradation preset {preset!r}")
