"""Where `render.py` actually puts each block on a generated label.

Measured from the rendered pixels, not guessed. These boxes exist so the robustness
tests can ask "is *this* field readable" rather than "is the picture nice", which is the
whole of LP-192 and TC-12.

**They are coupled to the renderer's layout on purpose.** A region box that quietly
stopped containing its text would make every readability assertion pass for the wrong
reason — a green suite proving nothing, which is worse than a red one. `test_robustness`
asserts each band still contains print, so moving a block in `render.py` fails loudly
here instead of silently downgrading the robustness set.

These are not a claim about real labels. On a photograph the evidence regions come from
the extractor, per field, and they move with the artwork.
"""

from __future__ import annotations

from api.models import BoundingBox, FieldName

#: Vertical bands of a single-face render, as a fraction of image height. Full width,
#: because the generator centres every line.
FIELD_BANDS: dict[FieldName, BoundingBox] = {
    FieldName.BRAND_NAME: BoundingBox(x0=0.0, y0=0.055, x1=1.0, y1=0.115),
    FieldName.CLASS_TYPE: BoundingBox(x0=0.0, y0=0.150, x1=1.0, y1=0.190),
    FieldName.ALCOHOL_CONTENT: BoundingBox(x0=0.0, y0=0.260, x1=1.0, y1=0.292),
    FieldName.NET_CONTENTS: BoundingBox(x0=0.0, y0=0.290, x1=1.0, y1=0.315),
    FieldName.PRODUCER: BoundingBox(x0=0.0, y0=0.370, x1=1.0, y1=0.398),
    FieldName.GOVERNMENT_WARNING: BoundingBox(x0=0.0, y0=0.450, x1=1.0, y1=0.540),
}

#: A band with nothing printed in it — used to check that "blank" and "unreadable" stay
#: distinguishable, since one is Missing and the other is Unreadable.
BLANK_BAND = BoundingBox(x0=0.0, y0=0.60, x1=1.0, y1=0.75)

#: The government warning's band alone, as a plain tuple for the degradation helpers.
WARNING_BAND: tuple[float, float] = (
    FIELD_BANDS[FieldName.GOVERNMENT_WARNING].y0,
    FIELD_BANDS[FieldName.GOVERNMENT_WARNING].y1,
)
