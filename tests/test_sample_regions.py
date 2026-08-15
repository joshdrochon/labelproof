"""The evidence boxes a sample draws must land on the text they name (LP-350).

The demo is the first thing a reviewer clicks, and the panel above it promises "outlined
areas are where each checked value was read". A box on blank paper makes that caption a
lie about the one thing this product sells: that it can show you where it looked.

The fake provider carried its own hand-guessed table of regions while
`fixtures/generator/layout.py` carried a measured one. They disagreed about every field
and about the government warning by a fifth of the image height — the guess put it at
0.66–0.88, which is inside `BLANK_BAND`, the region defined as having nothing printed in
it. Two tables describing one layout, and only one of them was measured.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from api.models import FieldName
from api.provider.fake import _APPROX_REGIONS
from fixtures.generator.build import LABELS
from fixtures.generator.layout import BLANK_BAND, FIELD_BANDS

CLEAN = LABELS / "tc01_old_tom_clean.png"


def _ink_fraction(image: Image.Image, y0: float, y1: float) -> float:
    """How much of this horizontal band is darker than the paper around it."""
    pixels = np.asarray(image.convert("L"), dtype=np.int16)
    height = pixels.shape[0]
    band = pixels[int(y0 * height) : max(int(y1 * height), int(y0 * height) + 1)]
    paper = int(np.percentile(pixels, 90))
    return float((band < paper - 40).mean())


def test_the_sample_regions_are_the_measured_ones() -> None:
    """One table, not two. A second copy is a copy that drifts, and this one drifted into
    the blank band."""
    assert _APPROX_REGIONS == dict(FIELD_BANDS)


@pytest.mark.parametrize("field", sorted(FIELD_BANDS, key=lambda f: f.value))
def test_every_sample_region_contains_print(field: FieldName) -> None:
    """The box has to sit on the words. Asserted against the rendered pixels rather than
    against another table, because agreeing with a second guess is not evidence."""
    box = _APPROX_REGIONS[field]
    ink = _ink_fraction(Image.open(CLEAN), box.y0, box.y1)

    assert ink > 0.005, (
        f"the evidence box for {field.value} covers y {box.y0:.3f}-{box.y1:.3f}, which is "
        f"{ink:.1%} ink - it is pointing at blank paper. The demo's caption promises "
        f"these are where each value was read."
    )


def test_no_sample_region_sits_in_the_band_that_is_deliberately_empty() -> None:
    """`BLANK_BAND` exists so "nothing printed here" stays distinguishable from "could not
    read this". An evidence box inside it is pointing at the one place guaranteed to have
    nothing — which is exactly where the government warning's box used to land."""
    for field, box in _APPROX_REGIONS.items():
        overlap = min(box.y1, BLANK_BAND.y1) - max(box.y0, BLANK_BAND.y0)
        assert overlap <= 0, (
            f"{field.value}'s box overlaps BLANK_BAND by {overlap:.3f} of image height"
        )
