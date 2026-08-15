"""Where `render.py` actually puts each block on a generated label.

Measured from the rendered pixels, not guessed. These boxes exist so the robustness
tests can ask "is *this* field readable" rather than "is the picture nice", which is the
whole of LP-192 and TC-12.

**They are coupled to the renderer's layout on purpose.** A region box that quietly
stopped containing its text would make every readability assertion pass for the wrong
reason — a green suite proving nothing, which is worse than a red one. `test_robustness`
asserts each band still contains print, so moving a block in `render.py` fails loudly
here instead of silently downgrading the robustness set.

**One table per face, because `render.py` lays out more than one.** A back face omits the
brand and class/type blocks and everything below them moves up; a single table applied to
both is a table that is wrong about one of them. `BANDS_BY_FACE` is the only lookup a
caller should use, and a face it does not name has no measured layout — draw nothing.

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

#: The front face of a two-image render, which is the single-face flow stopped early.
#:
#: `render.py` skips the producer and the warning when `face == "front"` but draws
#: everything above them at the same offsets, so these are `FIELD_BANDS`' own numbers
#: rather than a second measurement. Kept as its own table, holding only the fields the
#: front actually carries, so that "every band lands on ink on the face it names" is a
#: statement the tests can check field by field instead of one with exceptions.
FRONT_FIELD_BANDS: dict[FieldName, BoundingBox] = {
    field: FIELD_BANDS[field]
    for field in (
        FieldName.BRAND_NAME,
        FieldName.CLASS_TYPE,
        FieldName.ALCOHOL_CONTENT,
        FieldName.NET_CONTENTS,
    )
}

#: Vertical bands of the *back* face of a two-image render. Measured from the pixels of
#: `fixtures/labels/tc16_front_back_back.png`, the same way the single-face table was.
#:
#: A back face is not the bottom of a single face. `render.py` skips the brand name and
#: the class/type block entirely when `face == "back"`, so everything below them starts at
#: the top margin instead and the whole column shifts up by roughly a fifth of the image
#: height — the warning lands at 0.27-0.35 here against 0.45-0.54 on a single face.
#:
#: Applying the single-face table to a back render is what put the demo's government
#: warning outline on blank paper under a caption promising "outlined areas are where each
#: checked value was read". Two faces, two layouts, and only one of them had a table.
BACK_FIELD_BANDS: dict[FieldName, BoundingBox] = {
    FieldName.ALCOHOL_CONTENT: BoundingBox(x0=0.0, y0=0.074, x1=1.0, y1=0.104),
    FieldName.NET_CONTENTS: BoundingBox(x0=0.0, y0=0.102, x1=1.0, y1=0.126),
    FieldName.PRODUCER: BoundingBox(x0=0.0, y0=0.184, x1=1.0, y1=0.211),
    FieldName.GOVERNMENT_WARNING: BoundingBox(x0=0.0, y0=0.262, x1=1.0, y1=0.352),
}

#: Which measured table describes which face, keyed by the `role` an image carries.
#:
#: The lookup is deliberately total-by-membership rather than by fallback: a role this map
#: does not name is a layout nobody measured, and a caller that cannot find one must draw
#: no box at all. A missing outline says "we did not locate this"; a borrowed one says
#: "we read it here" about a place the text is not.
BANDS_BY_FACE: dict[str, dict[FieldName, BoundingBox]] = {
    "single": FIELD_BANDS,
    "front": FRONT_FIELD_BANDS,
    "back": BACK_FIELD_BANDS,
}

#: A band with nothing printed in it — used to check that "blank" and "unreadable" stay
#: distinguishable, since one is Missing and the other is Unreadable. Blank on every face
#: in `BANDS_BY_FACE`, which is what lets it stand as the "pointing at nothing" tripwire
#: for all of them.
BLANK_BAND = BoundingBox(x0=0.0, y0=0.60, x1=1.0, y1=0.75)

#: The government warning's band alone, as a plain tuple for the degradation helpers.
WARNING_BAND: tuple[float, float] = (
    FIELD_BANDS[FieldName.GOVERNMENT_WARNING].y0,
    FIELD_BANDS[FieldName.GOVERNMENT_WARNING].y1,
)
