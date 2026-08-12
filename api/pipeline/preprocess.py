"""Photometric correction and the ordered preprocessing pass (IMG-2, IMG-3, IMG-6).

Geometry lives in `deskew`. This module handles light: lifting a photograph that is too
dim to read, and recovering what can honestly be recovered from one with glare on it.

**Glare is enhanced, never inpainted (IMG-3, IMG-5, LP-191).** A pixel at 255 carries no
information about what was underneath it. Filling that area with plausible label content
is fabrication with extra steps, and this product's whole argument is that it does not
invent values. Inpainting would also fail in the most expensive possible way: it produces
*confident* pixels, so the extractor reads clean text off a region where the label was
never visible, and the resulting verdict is a false pass with evidence attached.

So what happens instead is: recover detail in the near-saturated shoulder around a
highlight, restore the blown core to exactly the pixels it arrived as, and publish a mask
of it so per-field readability can mark whatever sits underneath Unreadable. The
government warning under a flash reflection comes back Unreadable and the brand on the dry
half of the label still comes back verified — TC-12.

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

from api.models import BoundingBox, ImageQuality
from api.pipeline import deskew as deskew_mod
from api.pipeline import quality as quality_mod

#: Luminance at or above which a pixel is treated as blown — no detail survives, and none
#: will be invented. Imported rather than restated: the score that reports glare and the
#: mask that acts on it have to mean the same thing, and a second copy of the number is a
#: second thing to get wrong.
BLOWN_LEVEL = quality_mod.BLOWN_LEVEL

#: CLAHE tile grid. Small enough to lift a shadowed corner independently of a lit one,
#: large enough that a tile still contains whole letters rather than parts of strokes.
_CLAHE_TILES = (8, 8)

#: CLAHE clip limit for a dim image. Conservative: the aim is to make text readable, not
#: to make the photograph look good. Above ~3 the noise in a dim phone photo is amplified
#: into speckle that the blur measure then reads as detail.
_CLAHE_CLIP_DIM = 2.0

#: …and for glare recovery, which touches a narrower tonal range and needs less push.
_CLAHE_CLIP_GLARE = 1.5

#: Blown-out share of the frame below which glare recovery is not attempted. A stray
#: specular pixel on a foil capsule is not glare, and running a local operator across the
#: whole image to chase it changes every pixel for no gain.
_GLARE_ENHANCE_FRACTION = 0.005


@dataclass(frozen=True)
class Preprocessed:
    """One image, ready for extraction, plus an account of everything done to it.

    `quality_before` is the assessment of the image as it arrived. It is the honest
    record: after normalization the scores necessarily look better, and reporting the
    improved numbers to an agent would be telling them the photograph was fine when it
    was not.
    """

    image: np.ndarray
    original: np.ndarray
    """The pixels exactly as they arrived, kept so a prominence check can still see them.

    Not a luxury. Lifting a *dim* photograph raises the warning band's contrast relative
    to the rest of the label — measured on TC-06's fixture, 0.379 before and 0.521 after —
    so a WARN-5 prominence judgement made on the processed image would see a violation as
    less severe than it is. The rules need the photograph, not our improved copy of it, and
    "carried forward in the report" has to mean pixels rather than a pair of whole-image
    scores that say nothing about one band.
    """

    quality_before: ImageQuality
    quality_after: ImageQuality
    rotation_deg: float = 0.0
    perspective_applied: bool = False
    exposure_normalized: bool = False
    glare_enhanced: bool = False
    glare_fraction: float = 0.0
    geometry: deskew_mod.Deskewed | None = None
    """The geometric pass, kept so callers can follow their coordinates through it."""

    notes: list[str] = field(default_factory=list)

    def region_before(self, box: BoundingBox) -> np.ndarray:
        """The same region, cut from the image as uploaded rather than as improved.

        `box` is in the *preprocessed* frame, matching everything else that handles
        evidence regions, and is carried backwards through the geometry here so callers
        never have to hold two coordinate systems at once.
        """
        from api.pipeline.quality import crop

        if self.geometry is None or self.geometry.transform is None:
            return crop(self.original, box)

        inverse = deskew_mod.Deskewed(
            image=self.original,
            transform=np.linalg.inv(np.asarray(self.geometry.transform, dtype=np.float64)),
            source_size=self.image.shape[:2],
        )
        return crop(self.original, inverse.map_box(box))

    def map_box(self, box: BoundingBox) -> BoundingBox:
        """Carry a box from the uploaded frame into the preprocessed one.

        Only geometry moves pixels; lifting exposure and recovering glare change values in
        place. So this is the deskew pass's mapping and nothing else, and it is the
        difference between an evidence box that points at the government warning and one
        that points at the blank stock beside it.
        """
        return self.geometry.map_box(box) if self.geometry else box

    @property
    def changed(self) -> bool:
        return (
            self.rotation_deg != 0.0
            or self.perspective_applied
            or self.exposure_normalized
            or self.glare_enhanced
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


def glare_mask(image: np.ndarray) -> np.ndarray:
    """Pixels that are blown out — where the label is, as far as anyone can tell, gone.

    Dilated slightly, because the ring immediately around a specular highlight is
    compressed to within a hair of saturation, and a letter stroke read out of it is a
    guess dressed up as a reading.
    """
    blown = (_to_gray(image) >= BLOWN_LEVEL).astype(np.uint8) * 255
    return cv2.dilate(blown, np.ones((5, 5), np.uint8), iterations=1)


def blown_fraction(image: np.ndarray) -> float:
    """Share of the frame with no recoverable detail left in it."""
    gray = _to_gray(image)
    return float((gray >= BLOWN_LEVEL).sum()) / gray.size


def enhance_glare(image: np.ndarray) -> np.ndarray:
    """Recover the shoulder around a highlight. Never fill the highlight itself.

    The blown pixels are written back from the input afterwards, so this cannot invent
    label content whatever the local operator does to their neighbourhood — a property
    the tests assert directly on the pixels rather than infer from the code.
    """
    blown = _to_gray(image) >= BLOWN_LEVEL
    enhanced = _apply_to_luminance(image, _CLAHE_CLIP_GLARE)
    enhanced[blown] = image[blown]
    return enhanced


def preprocess(image: np.ndarray, *, allow_perspective: bool = True) -> Preprocessed:
    """The ordered pass: geometry, then exposure, then glare (the build spec step 6).

    Geometry first because both photometric steps are local operators over tiles, and a
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

    fraction = blown_fraction(working)
    glare_enhanced = False
    if fraction >= _GLARE_ENHANCE_FRACTION:
        working = enhance_glare(working)
        glare_enhanced = True
        notes.append(
            f"recovered detail around glare covering {fraction:.1%} of the image; the "
            f"blown area itself is untouched and nothing was painted into it"
        )
    else:
        notes.append("no glare worth recovering from")

    return Preprocessed(
        image=working,
        original=image,
        quality_before=before,
        quality_after=quality_mod.assess(working),
        rotation_deg=geometry.rotation_deg,
        perspective_applied=geometry.perspective_applied,
        exposure_normalized=exposure_normalized,
        glare_enhanced=glare_enhanced,
        glare_fraction=round(fraction, 4),
        geometry=geometry,
        notes=notes,
    )
