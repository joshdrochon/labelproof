"""The evidence boxes a sample draws must land on the text they name (LP-350).

The demo is the first thing a reviewer clicks, and the panel above it promises "outlined
areas are where each checked value was read". A box on blank paper makes that caption a
lie about the one thing this product sells: that it can show you where it looked.

The fake provider carried its own hand-guessed table of regions while
`fixtures/generator/layout.py` carried a measured one. They disagreed about every field
and about the government warning by a fifth of the image height — the guess put it at
0.66–0.88, which is inside `BLANK_BAND`, the region defined as having nothing printed in
it. Two tables describing one layout, and only one of them was measured.

Deleting the guess left a second version of the same lie standing. One measured table was
applied to every image regardless of which face it showed, and a back face is not the
bottom of a single face: `render.py` drops the brand and class/type blocks, so the warning
sits at 0.27–0.35 there while the single-face box points at 0.45–0.54, bare paper below
the paragraph. The old suite was green throughout, because it only ever opened
`tc01_old_tom_clean.png`, a single-face render. A back was never checked, so the one face
that was wrong was the one face nothing looked at.

Hence the parametrisation below: every face the provider will serve a table for, checked
against a render of that face. A fourth layout cannot be added to `_REGIONS_BY_FACE`
without a reference render to prove it, because the lookup that finds one raises.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from api.models import FieldName
from api.provider.fake import _REGIONS_BY_FACE, _regions_for
from fixtures.generator.build import LABELS
from fixtures.generator.layout import BANDS_BY_FACE, BLANK_BAND

#: A render of each face the provider has a table for. Keyed by the same role string the
#: pipeline puts on `ImageInput`, so the test asks its question in the provider's terms.
FACE_RENDERS: dict[str, Path] = {
    "single": LABELS / "tc01_old_tom_clean.png",
    "front": LABELS / "tc16_front_back_front.png",
    "back": LABELS / "tc16_front_back_back.png",
}


def _ink_fraction(image: Image.Image, y0: float, y1: float) -> float:
    """How much of this horizontal band is darker than the paper around it."""
    pixels = np.asarray(image.convert("L"), dtype=np.int16)
    height = pixels.shape[0]
    band = pixels[int(y0 * height) : max(int(y1 * height), int(y0 * height) + 1)]
    paper = int(np.percentile(pixels, 90))
    return float((band < paper - 40).mean())


def _render_for(face: str) -> Image.Image:
    """The image a face's bands are checked against.

    A face with no render here fails rather than skips. A table nobody can check is a
    table that gets to be wrong, which is exactly how the back face stayed broken.
    """
    path = FACE_RENDERS.get(face)
    assert path is not None, (
        f"{face!r} has a band table but no render to check it against. Add a fixture "
        f"showing that face to FACE_RENDERS - an unchecked table is a table free to point "
        f"at blank paper, which is how the back face broke."
    )
    return Image.open(path)


def _cases() -> list[tuple[str, FieldName]]:
    return [
        (face, field)
        for face in sorted(_REGIONS_BY_FACE)
        for field in sorted(_REGIONS_BY_FACE[face], key=lambda f: f.value)
    ]


def test_the_sample_regions_are_the_measured_ones() -> None:
    """One set of tables, not two. A second copy is a copy that drifts, and the first one
    drifted into the blank band."""
    assert _REGIONS_BY_FACE == BANDS_BY_FACE


@pytest.mark.parametrize(("face", "field"), _cases(), ids=lambda v: getattr(v, "value", v))
def test_every_sample_region_contains_print(face: str, field: FieldName) -> None:
    """The box has to sit on the words, on the face it describes. Asserted against the
    rendered pixels rather than against another table, because agreeing with a second
    guess is not evidence."""
    box = _REGIONS_BY_FACE[face][field]
    ink = _ink_fraction(_render_for(face), box.y0, box.y1)

    assert ink > 0.005, (
        f"on the {face} face the evidence box for {field.value} covers y "
        f"{box.y0:.3f}-{box.y1:.3f}, which is {ink:.1%} ink - it is pointing at blank "
        f"paper. The demo's caption promises these are where each value was read."
    )


@pytest.mark.parametrize("face", sorted(_REGIONS_BY_FACE))
def test_no_sample_region_sits_in_the_band_that_is_deliberately_empty(face: str) -> None:
    """`BLANK_BAND` exists so "nothing printed here" stays distinguishable from "could not
    read this". An evidence box inside it is pointing at the one place guaranteed to have
    nothing — which is exactly where the government warning's box used to land."""
    for field, box in _REGIONS_BY_FACE[face].items():
        overlap = min(box.y1, BLANK_BAND.y1) - max(box.y0, BLANK_BAND.y0)
        assert overlap <= 0, (
            f"{face} {field.value}'s box overlaps BLANK_BAND by {overlap:.3f} of image "
            f"height"
        )


@pytest.mark.parametrize("face", sorted(_REGIONS_BY_FACE))
def test_the_blank_band_really_is_blank_on_every_face(face: str) -> None:
    """The tripwire above is only worth anything if the band it names has nothing in it on
    the face being checked. A layout that printed something at 0.60–0.75 would turn the
    check into a formality that passes for the wrong reason."""
    ink = _ink_fraction(_render_for(face), BLANK_BAND.y0, BLANK_BAND.y1)
    assert ink < 0.001, f"BLANK_BAND is {ink:.1%} ink on the {face} face"


def test_a_face_nobody_measured_gets_no_box_at_all() -> None:
    """Three images, or a role the uploader made up, and `default_roles` returns None —
    the pipeline is saying it does not know which face this is. The honest response is no
    outline. A box borrowed from some other layout would tell the reviewer we read the
    value in a place the value is not, which is the failure this whole file exists to
    prevent."""
    assert _regions_for(None) == {}
    assert _regions_for("side") == {}
