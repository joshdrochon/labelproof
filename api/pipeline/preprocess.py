"""Photometric correction and the ordered preprocessing pass (IMG-2, IMG-6).

Geometry lives in `deskew`. This module handles light: lifting a photograph that is too
dim to read.

**Normalization is remedial, never cosmetic — and that is a correctness rule, not taste.**
The government warning has a prominence requirement (WARN-5): a statement printed in pale
grey on cream is a violation even though every word is present, and TC-06 is exactly that
label. If preprocessing lifted contrast on every image, a buried warning would arrive at
the extractor looking perfectly legible, the low-contrast signal would never fire, and a
real violation would be reported as a pass. So contrast is only touched on images that are
*measurably* underexposed, and the measurements taken before any lifting are carried
forward in the report so the rules can still see what the photograph actually looked like.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from api.models import ImageQuality
from api.pipeline import deskew as deskew_mod
from api.pipeline import quality as quality_mod

#: CLAHE tile grid. Small enough to lift a shadowed corner independently of a lit one,
#: large enough that a tile still contains whole letters rather than parts of strokes.
_CLAHE_TILES = (8, 8)

#: CLAHE clip limit for a dim image. Conservative: the aim is to make text readable, not
#: to make the photograph look good. Above ~3 the noise in a dim phone photo is amplified
#: into speckle that the blur measure then reads as detail.
_CLAHE_CLIP_DIM = 2.0


@dataclass(frozen=True)
class Preprocessed:
    """One image, ready for extraction, plus an account of everything done to it.

    `quality_before` is the assessment of the image as it arrived. It is the honest
    record: after normalization the scores necessarily look better, and reporting the
    improved numbers to an agent would be telling them the photograph was fine when it
    was not.
    """

    image: np.ndarray
    quality_before: ImageQuality
    quality_after: ImageQuality
    rotation_deg: float = 0.0
    perspective_applied: bool = False
    exposure_normalized: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return (
            self.rotation_deg != 0.0
            or self.perspective_applied
            or self.exposure_normalized
        )


def _to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)


def _apply_to_luminance(image: np.ndarray, clip: float) -> np.ndarray:
    """Run CLAHE on luminance only, leaving hue and saturation untouched.

    Equalising the RGB channels independently shifts colour, and colour is evidence here:
    a warning printed in pale grey and one printed in black are different compliance
    outcomes, and the difference must survive preprocessing intact.
    """
    clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=_CLAHE_TILES)
    if image.ndim == 2:
        return clahe.apply(image)

    lab = cv2.cvtColor(image, cv2.COLOR_RGB2LAB)
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def normalize_exposure(image: np.ndarray) -> np.ndarray:
    """Lift a dim photograph until the text is readable (IMG-2, TC-13).

    Local rather than global. A global curve on a photo that is dark in one corner and
    lit in another either leaves the dark corner unreadable or blows out the lit one —
    which is the ordinary case for a bottle photographed under one lamp.
    """
    return _apply_to_luminance(image, _CLAHE_CLIP_DIM)


def needs_exposure_normalization(assessment: ImageQuality) -> bool:
    """Only images that are measurably too dark. See the module docstring for why.

    The line is `EXPOSURE_FLOOR` itself — `exposure_score` is mean luminance over that
    floor, clipped at 1.0, so a score below 1.0 means "dimmer than a well-lit label" and
    exactly 1.0 means "no darker than one". No second threshold: a label photographed in
    normal light scores 1.0 and is never touched, which is what keeps TC-06's deliberately
    buried warning buried.
    """
    return assessment.exposure < 1.0


def preprocess(image: np.ndarray, *, allow_perspective: bool = True) -> Preprocessed:
    """The ordered pass: geometry, then exposure (BUILD.md §6 step 6).

    Geometry first because the photometric step is a local operator over tiles, and a
    tile that straddles the label edge and the desk it is lying on is measuring two
    different scenes. Rectifying first means the tiles see label.
    """
    before = quality_mod.assess(image)
    notes: list[str] = []

    geometry = deskew_mod.correct(image, allow_perspective=allow_perspective)
    notes.append(geometry.note)
    working = geometry.image

    exposure_normalized = False
    if needs_exposure_normalization(before):
        candidate = normalize_exposure(working)
        if quality_mod.exposure_score(candidate) > quality_mod.exposure_score(working):
            working, exposure_normalized = candidate, True
            notes.append("lifted a dim photograph so the text is readable")
        else:
            notes.append("normalization did not improve the exposure — reverted")
    else:
        notes.append("exposure left alone — the photograph is not underexposed")

    return Preprocessed(
        image=working,
        quality_before=before,
        quality_after=quality_mod.assess(working),
        rotation_deg=geometry.rotation_deg,
        perspective_applied=geometry.perspective_applied,
        exposure_normalized=exposure_normalized,
        notes=notes,
    )
