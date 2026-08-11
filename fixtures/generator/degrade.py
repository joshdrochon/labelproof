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

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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


def motion_blur(image: np.ndarray, length: int = 25, angle: float = 12.0) -> np.ndarray:
    """Camera shake — smeared along one direction (TC-14).

    Directional rather than radial, which is what a hand-held shot at a slow shutter
    actually produces. A set containing only defocus would leave the blur measure
    untested against the blur people are most likely to send.
    """
    size = max(3, int(length) | 1)
    kernel = np.zeros((size, size), np.float32)
    kernel[size // 2, :] = 1.0
    matrix = cv2.getRotationMatrix2D((size / 2 - 0.5, size / 2 - 0.5), angle, 1.0)
    kernel = cv2.warpAffine(kernel, matrix, (size, size))
    total = kernel.sum()
    kernel = kernel / total if total else kernel
    return cv2.filter2D(image, -1, kernel, borderType=cv2.BORDER_REPLICATE)


def dim(image: np.ndarray, factor: float = 0.35) -> np.ndarray:
    """Underexposed but recoverable (TC-13). Scales luminance without crushing to black."""
    return np.clip(image.astype(np.float32) * factor, 0, 255).astype(np.uint8)


def side_lit(
    image: np.ndarray, *, bright: float = 0.95, dark: float = 0.18
) -> np.ndarray:
    """A bottle under one lamp: bright on one side, in shadow on the other (TC-13).

    The realistic form of bad lighting, and the one that separates local normalization
    from a brightness slider — a global curve either leaves the shadowed half unreadable
    or blows out the lit half.
    """
    h, w = image.shape[:2]
    ramp = np.linspace(bright, dark, w, dtype=np.float32)
    gain = np.tile(ramp, (h, 1))[..., None]
    return np.clip(image.astype(np.float32) * gain, 0, 255).astype(np.uint8)


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


# --------------------------------------------------------------------------------------
# The robustness set (LP-195 – LP-198, LP-201)
# --------------------------------------------------------------------------------------
#
# Each condition names what the pipeline is required to do with it, because a robustness
# fixture with no stated expectation only proves the code did not crash. The three
# outcomes are deliberately different obligations:
#
#   readable          every field still verifies — correct the image, do not reject it
#   warning_illegible the warning region alone is unreadable; the rest still verifies
#   pregated          the image is hopeless: retake reason, zero model calls (LP-321)


@dataclass(frozen=True)
class Condition:
    """One degradation, with the behaviour it is there to pin down."""

    name: str
    tc: str
    description: str
    expectation: Literal["readable", "warning_illegible", "pregated"]
    why: str

    def apply(self, image: np.ndarray) -> np.ndarray:
        return apply_preset(image, self.name)


CONDITIONS: list[Condition] = [
    Condition(
        name="tc11_angle_15",
        tc="TC-11",
        description="photographed 15° off-axis on a desk",
        expectation="readable",
        why=(
            "A mild angle is the most common real defect and the least excusable to fail "
            "on. Composited on a surface rather than warped edge-to-edge, because "
            "rectification needs a boundary to find and a borderless warp has none."
        ),
    ),
    Condition(
        name="tc11_angle_30",
        tc="TC-11",
        description="photographed 30° off-axis on a desk",
        expectation="readable",
        why="The angle the PRD names for TC-11.",
    ),
    Condition(
        name="tc11_angle_45",
        tc="TC-11",
        description="photographed 45° off-axis on a desk",
        expectation="readable",
        why=(
            "Past what anyone would call a reasonable photo. Included to find the point "
            "where correction stops working, rather than to claim it always does."
        ),
    ),
    Condition(
        name="tc11_rotate_8",
        tc="TC-11",
        description="held 8° crooked, square to the label",
        expectation="readable",
        why=(
            "In-plane rotation is a different defect from perspective and takes different "
            "machinery. A set with only perspective cases would leave deskew untested."
        ),
    ),
    Condition(
        name="tc12_glare_warning",
        tc="TC-12",
        description="flash reflection across the government warning",
        expectation="warning_illegible",
        why=(
            "The case the whole per-region design exists for. One global quality number "
            "calls this image fine, because it is fine everywhere except the one place "
            "that matters most. The warning must come back Unreadable and the brand two "
            "inches above it must still be verified."
        ),
    ),
    Condition(
        name="tc12_glare_corner",
        tc="TC-12",
        description="specular highlight on the shoulder of the bottle, off the text",
        expectation="readable",
        why=(
            "The control for TC-12. Glare that lands on bare label stock must change "
            "nothing. Without it, a scorer that marked every image with a bright patch "
            "as damaged would pass the glare case and look correct."
        ),
    ),
    Condition(
        name="tc12_glare_total",
        tc="TC-12",
        description="flash across most of the label",
        expectation="pregated",
        why=(
            "Past recovery. The obligation here is a retake reason and zero model calls "
            "— spending a token on this image buys nothing, and inpainting it would buy "
            "something worse than nothing."
        ),
    ),
    Condition(
        name="tc13_dim",
        tc="TC-13",
        description="underexposed but recoverable",
        expectation="readable",
        why=(
            "TC-13 proper. The obligation is to normalize and read it, not to reject it "
            "— an agent told to retake a photograph that was perfectly recoverable has "
            "been made slower by the tool, which is the complaint the product exists to "
            "answer."
        ),
    ),
    Condition(
        name="tc13_dim_uneven",
        tc="TC-13",
        description="lit from one side, half the label in shadow",
        expectation="readable",
        why=(
            "The realistic form of bad lighting: a bottle under one lamp. A global tone "
            "curve either leaves the shadowed half unreadable or blows out the lit half, "
            "so this is what separates local normalization from a brightness slider."
        ),
    ),
    Condition(
        name="tc13_near_black",
        tc="TC-13",
        description="so underexposed there is nothing left to recover",
        expectation="pregated",
        why=(
            "The other side of the line. Lifting this would amplify noise into something "
            "that looks like text, which is the failure mode where enhancement turns "
            "into invention."
        ),
    ),
    Condition(
        name="tc14_blur_mild",
        tc="TC-14",
        description="slightly soft, still legible",
        expectation="readable",
        why=(
            "The lower edge of the blur scale. A soft photograph is still worth reading, "
            "and rejecting it would be the tool making the agent slower — which is the "
            "complaint, not the fix."
        ),
    ),
    Condition(
        name="tc14_blur_hopeless",
        tc="TC-14",
        description="out of focus past legibility",
        expectation="pregated",
        why=(
            "TC-14 proper. Nothing here can be read, so the obligation is a retake reason "
            "and zero model calls. The dangerous failure is not rejecting it — it is an "
            "extractor confidently returning plausible field values from mush."
        ),
    ),
    Condition(
        name="tc14_blur_motion",
        tc="TC-14",
        description="camera shake — smeared in one direction",
        expectation="pregated",
        why=(
            "Directional, unlike a Gaussian, and the commoner defect in a hand-held shot. "
            "A defocus-only set would leave the blur measure untested against the blur "
            "people actually produce."
        ),
    ),
    Condition(
        name="lp201_cylinder",
        tc="LP-201",
        description="wrapped around a bottle",
        expectation="readable",
        why=(
            "Curvature is not a projective distortion, so the four-point transform cannot "
            "undo it and this fixture exists to prove we do not pretend otherwise. The "
            "obligation is that the label stays legible and the pass reports no "
            "correction — an audit trail that says what did not happen."
        ),
    ),
    Condition(
        name="lp201_cylinder_angled",
        tc="LP-201",
        description="bottle photographed slightly off-axis",
        expectation="readable",
        why=(
            "The realistic combination: nobody photographs a bottle both curved and "
            "perfectly square to the camera. Rectification has a boundary to find here, "
            "and it must not make the curvature worse in the process."
        ),
    ),
]

#: Fixture name -> one-line description. Kept as a flat mapping because it is the shape
#: the rest of the repo already reads.
PRESETS: dict[str, str] = {c.name: c.description for c in CONDITIONS}


def by_tc(tc: str) -> list[Condition]:
    """Every condition covering one canonical test case."""
    return [c for c in CONDITIONS if c.tc == tc]


def apply_preset(image: np.ndarray, preset: str) -> np.ndarray:
    match preset:
        case "tc11_angle_15":
            return on_surface(image, degrees=15.0)
        case "tc11_angle_30":
            return on_surface(image, degrees=30.0)
        case "tc11_angle_45":
            return on_surface(image, degrees=45.0)
        case "tc11_rotate_8":
            return rotate(image, 8.0)
        case "tc12_glare_warning":
            return glare_over_warning(image)
        case "tc12_glare_corner":
            return glare(image, centre=(0.5, 0.80), radius=0.09, aspect=2.0)
        case "tc12_glare_total":
            return glare(image, centre=(0.5, 0.45), radius=0.55, aspect=1.4)
        case "tc13_dim":
            return dim(image)
        case "tc13_dim_uneven":
            return side_lit(image)
        case "tc13_near_black":
            return dim(image, 0.04)
        case "tc14_blur_mild":
            return blur(image, 2.0)
        case "tc14_blur_hopeless":
            return blur(image, 12.0)
        case "tc14_blur_motion":
            return motion_blur(image)
        case "lp201_cylinder":
            return cylinder(image)
        case "lp201_cylinder_angled":
            return on_surface(cylinder(image), degrees=20.0)
        case _:
            raise KeyError(f"unknown degradation preset {preset!r}")


# --------------------------------------------------------------------------------------
# Building the set as files
# --------------------------------------------------------------------------------------

#: The compliant label every robustness fixture is degraded from. One base, so a
#: difference between two conditions is the degradation and nothing else.
BASE_FIXTURE = "tc01_old_tom_clean"


def build(directory: Path | None = None) -> dict[str, str]:
    """Render the robustness set to PNGs and return name -> sha256.

    Deterministic: same code in, byte-identical files out (LP-123). The digests are
    written alongside so a regeneration that silently changed a fixture shows up as a
    diff rather than as a mysteriously moved test result.

        python -m fixtures.generator.degrade
    """
    from PIL import Image

    from fixtures.generator.catalog import by_name
    from fixtures.generator.render import render

    target = directory or Path(__file__).resolve().parents[2] / "fixtures" / "robustness"
    target.mkdir(parents=True, exist_ok=True)

    base = np.array(render(by_name(BASE_FIXTURE)))
    digests: dict[str, str] = {}

    for condition in CONDITIONS:
        path = target / f"{condition.name}.png"
        Image.fromarray(condition.apply(base)).save(path, "PNG", optimize=True)
        digests[condition.name] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]

    manifest = {
        "base": BASE_FIXTURE,
        "note": (
            "Degradations simulate optics, not physics. A Gaussian is not lens blur and "
            "an overlay is not a specular highlight on curved glass. Reproducible, which "
            "regression tests need and photographs cannot give — the gap is Tier B's job."
        ),
        "conditions": [
            {
                "name": c.name,
                "tc": c.tc,
                "description": c.description,
                "expectation": c.expectation,
                "why": c.why,
                "sha256": digests[c.name],
            }
            for c in CONDITIONS
        ],
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return digests


def main() -> int:
    digests = build()
    print(f"rendered {len(digests)} robustness fixtures")
    for name, digest in digests.items():
        print(f"  {name:24s} {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
